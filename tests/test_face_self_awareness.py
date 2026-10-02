"""E.V. knows it has a face, and uses it.

Before this, the face only ever showed the loop's own states - thinking,
speaking, a smile on success - and the model had no idea it existed. Asked
for a demo it had nothing to run, and asked to look angry it could only say
so. Four layers, tested alone:

* the schema - `chat` carries a `mood`, and every mood it offers is real
* `ev.face.expression` - an emote outlasts the loop's states, and the demo
  reel plays every mood once and stops when the user talks
* `ev_core` - a reply's mood reaches the face after "speaking", replaces the
  stock success smile, and the model is told when there is no face to show
* `ev.face.window` - a demo keeps the face up and captions each mood
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
import ev_core  # noqa: E402
from ev.brain import ToolCall  # noqa: E402
from ev.face import link  # noqa: E402
from ev.face.expression import REEL_SKIP, Expression, apply_command, load_library  # noqa: E402
from ev.face.link import FaceLink  # noqa: E402
from ev.session import Session  # noqa: E402
from tools.base import ToolResult  # noqa: E402
from tools.schemas import FACE_MOODS, TOOL_SPECS, to_gemini_tools, to_openai_tools  # noqa: E402

HAS_QT = importlib.util.find_spec("PySide6") is not None


# -- the schema ---------------------------------------------------------------


def _chat_spec() -> dict:
    return next(spec for spec in TOOL_SPECS if spec["name"] == "chat")


def test_chat_can_choose_a_mood():
    mood = _chat_spec()["parameters"]["properties"]["mood"]
    assert mood["enum"] == list(FACE_MOODS)
    # Optional: a reply with no mood leaves the face to the loop.
    assert "mood" not in _chat_spec()["parameters"]["required"]


def test_every_offered_mood_exists_on_the_face():
    """A mood the face does not have would be silently ignored."""
    library = load_library()
    missing = [m for m in FACE_MOODS if m != "demo" and m not in library.moods]
    assert missing == []


def test_the_loops_own_states_are_not_offered():
    """Wearing 'listening' or the red lockdown alert would be a lie."""
    for state in ("listening", "speaking", "focused", "alert", "idle"):
        assert state not in FACE_MOODS


def test_both_providers_receive_the_mood():
    openai_chat = next(t for t in to_openai_tools() if t["function"]["name"] == "chat")
    assert "mood" in openai_chat["function"]["parameters"]["properties"]
    gemini = to_gemini_tools()[0]["functionDeclarations"]
    gemini_chat = next(t for t in gemini if t["name"] == "chat")
    assert gemini_chat["parameters"]["properties"]["mood"]["enum"] == list(FACE_MOODS)


def test_the_prompt_tells_e_v_it_has_a_face():
    assert "You have a face" in config.SYSTEM_PROMPT
    assert "demo" in config.SYSTEM_PROMPT


# -- the expression -----------------------------------------------------------


def _run(expression: Expression, seconds: float, step: float = 0.05) -> None:
    for _ in range(int(seconds / step)):
        expression.tick(step)


def test_an_emote_outlasts_the_loops_states_then_hands_back():
    face = Expression(mood="idle")
    apply_command(face, json.dumps({"emote": "angry", "hold": 2}))
    face.tick(0.05)
    assert face.mood == "angry"
    # Speaking, then listening, arrive underneath it - and wait.
    apply_command(face, json.dumps({"event": "speaking"}))
    apply_command(face, json.dumps({"event": "listening"}))
    _run(face, 1.0)
    assert face.mood == "angry"
    _run(face, 1.5)
    assert face.mood == "listening"


def test_a_base_mood_can_be_worn_on_demand_without_sticking():
    """"Look sleepy" must not leave E.V. asleep."""
    face = Expression(mood="idle")
    assert face.emote("sleepy", 1.0)
    face.tick(0.05)
    assert face.mood == "sleepy"
    _run(face, 1.2)
    assert face.mood == "idle"


def test_an_unknown_emote_is_ignored_not_drawn_as_neutral():
    face = Expression(mood="listening")
    assert face.emote("furious") is False
    face.tick(0.05)
    assert face.mood == "listening"


def test_the_demo_plays_every_mood_once_and_captions_each():
    face = Expression(mood="idle")
    shown: list[tuple[str, int, int]] = []
    face.on_reel = lambda name, i, total: shown.append((name, i, total))
    apply_command(face, json.dumps({"demo": True}))
    assert face.demo_running
    _run(face, 40)
    names = [name for name, _, _ in shown]
    assert names == [m for m in face.library.moods if m not in REEL_SKIP]
    assert shown[-1][1] == shown[-1][2] == len(names)
    assert not face.demo_running
    assert face.mood == "idle"


def test_the_user_talking_ends_the_demo():
    face = Expression(mood="idle")
    apply_command(face, json.dumps({"demo": True}))
    _run(face, 2.0)
    apply_command(face, json.dumps({"heard": "ok that's enough"}))
    _run(face, 3.0)
    assert not face.demo_running


def test_lockdown_cuts_through_a_demo():
    face = Expression(mood="idle")
    apply_command(face, json.dumps({"demo": True}))
    _run(face, 2.0)
    apply_command(face, json.dumps({"event": "lockdown"}))
    face.tick(0.05)
    assert face.mood == "alert"
    _run(face, 5.0)
    assert face.mood == "alert"


def test_the_new_moods_load_and_are_distinct():
    library = load_library()
    for name in ("angry", "excited", "smug", "wink", "worried", "bored"):
        assert name in library.moods
    wink = library.moods["wink"]
    assert wink.left.open != wink.right.open  # one eye, not two


# -- the link -----------------------------------------------------------------


def test_the_link_sends_emotes_and_demos(tmp_path, monkeypatch):
    out = tmp_path / "face.jsonl"
    script = f"import sys; open({str(out)!r}, 'w').write(sys.stdin.read())"
    real_popen = link.subprocess.Popen
    monkeypatch.setattr(
        link.subprocess, "Popen",
        lambda argv, **kw: real_popen([sys.executable, "-c", script], **kw),
    )
    monkeypatch.setattr(link.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv("DISPLAY", ":99")

    face = FaceLink()
    assert face.start()
    face.emote("angry", 3.0)
    face.emote("angry", 3.0)  # two scowls are two scowls
    face.demo()
    face.close()
    lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert lines.count({"emote": "angry", "hold": 3.0}) == 2
    assert {"demo": True} in lines


# -- the core -----------------------------------------------------------------


class RecordingFace:
    def __init__(self, alive: bool = True) -> None:
        self.log: list[tuple[str, object]] = []
        self.alive = alive
        self.hold = 0.0

    def event(self, name):
        self.log.append(("event", name))

    def show(self, on):
        self.log.append(("show", on))

    def heard(self, text):
        self.log.append(("heard", text))

    def said(self, text):
        self.log.append(("said", text))

    def emote(self, mood, hold_s=0.0):
        self.log.append(("emote", mood))
        self.hold = hold_s

    def demo(self):
        self.log.append(("demo", True))

    def close(self):
        pass


class QuietUI:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeSpeaker:
    enabled = False
    speaking = False

    async def say(self, text):
        return text

    def stop(self):
        pass


class FakeBrain:
    def __init__(self) -> None:
        self.remembered: list[str] = []

    def remember(self, command, speech, detail="", untrusted=False):
        self.remembered.append(speech)


def _assistant() -> ev_core.EV:
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = QuietUI()
    assistant.face = RecordingFace()
    assistant.speaker = FakeSpeaker()
    assistant.session = Session()
    assistant.brain = FakeBrain()
    assistant.mic = None
    assistant.text_mode = True
    assistant._running = True
    assistant._cancel_refused = None
    assistant.memory = type("M", (), {"touch": lambda self: None})()
    return assistant


def test_a_replys_mood_lands_after_speaking():
    assistant = _assistant()
    asyncio.run(assistant.say("Grr. Fine.", mood="angry"))
    log = assistant.face.log
    assert log.index(("event", "speaking")) < log.index(("emote", "angry"))
    assert 2.5 <= assistant.face.hold <= 10.0


def test_no_mood_leaves_the_face_alone():
    assistant = _assistant()
    asyncio.run(assistant.say("Chrome's up."))
    assert not any(kind == "emote" for kind, _ in assistant.face.log)


def test_demo_runs_the_reel():
    assistant = _assistant()
    asyncio.run(assistant.say("Here's the whole range.", mood="demo"))
    assert ("demo", True) in assistant.face.log
    assert not any(kind == "emote" for kind, _ in assistant.face.log)


def test_a_chosen_mood_replaces_the_success_smile():
    """"Look angry" answered with a grin is the face contradicting the words."""
    assistant = _assistant()

    async def run_tool(call):
        return ToolResult.success("Grr.")

    assistant._run_tool = run_tool
    call = ToolCall("chat", {"reply": "Grr.", "mood": "angry"})
    asyncio.run(assistant._execute("look angry", call))
    assert ("event", "success") not in assistant.face.log
    assert ("emote", "angry") in assistant.face.log


def test_a_chat_without_a_mood_still_smiles():
    assistant = _assistant()

    async def run_tool(call):
        return ToolResult.success("Done.")

    assistant._run_tool = run_tool
    asyncio.run(assistant._execute("hi", ToolCall("chat", {"reply": "Done."})))
    assert ("event", "success") in assistant.face.log


def test_the_model_is_told_when_its_face_is_not_on_screen():
    assistant = _assistant()
    assistant.face = RecordingFace(alive=False)
    assert "not on screen" in assistant._face_context()
    assistant.face = RecordingFace(alive=True)
    assert assistant._face_context() == ""


# -- the window ---------------------------------------------------------------


@pytest.fixture
def qt_app():
    if not HAS_QT:
        pytest.skip("PySide6 not installed")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    if not isinstance(app, QApplication):
        pytest.skip("a widget-less QGuiApplication already owns this process")
    return app


def _pump(ms: int) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_a_demo_keeps_the_face_up_and_captions_it(qt_app):
    from ev.face.window import FaceWindow, route_line

    expression = Expression(load_library())
    window = FaceWindow(expression, presence="summoned", busy_poll_s=0)
    window.start()
    assert not window.isVisible()

    line = json.dumps({"demo": True})
    apply_command(expression, line)
    route_line(window, line)
    _pump(300)
    assert window.isVisible()
    assert "(1/" in window.caption._said

    # The conversation lapses mid-reel; the demo is still shown through.
    route_line(window, json.dumps({"show": False}))
    _pump(50)
    assert window.isVisible()
    window.close()
