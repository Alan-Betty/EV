"""The face as part of E.V.: started by the core, told what is happening,
in view for a conversation and out of it when the screen is busy.

Four layers, each tested alone:

* `ev.face.busy` - is now a bad moment to be seen? (fake command runner)
* `ev.face.link` - the pipe, which must never block or raise (fake process)
* `ev_core` - what the loop tells the face, and when (recording face)
* `ev.face.window` - presence and captions (offscreen Qt; skipped without it)
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

import ev_core  # noqa: E402
from ev.face import busy, link  # noqa: E402
from ev.face.link import FaceLink  # noqa: E402
from ev.session import Session  # noqa: E402
from ev.stt import Transcript  # noqa: E402

HAS_QT = importlib.util.find_spec("PySide6") is not None


# -- busy ---------------------------------------------------------------


def _runner(answers: dict[str, str | None]):
    """Answer a probe by the first key that appears in its argv."""
    def run(argv: list[str]) -> str | None:
        joined = " ".join(argv)
        for key, value in answers.items():
            if key in joined:
                return value
        return None
    return run


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(busy, "IS_WINDOWS", False)
    monkeypatch.setattr(busy, "IS_LINUX", True)


INHIBITOR = "/org/gnome/SessionManager/Inhibitor4"


def _session(reason: str, flags: int, app: str = "brave") -> dict[str, str | None]:
    return {
        "IsInhibited": "(true,)",
        "GetInhibitors": f"([objectpath '{INHIBITOR}'],)",
        "GetFlags": f"(uint32 {flags},)",
        "GetReason": f"('{reason}',)",
        "GetAppId": f"('{app}',)",
        "show-banners": "true\n",
    }


def test_nothing_happening_is_not_busy(linux):
    run = _runner({"show-banners": "true\n", "IsInhibited": "(false,)"})
    assert busy.busy_reason(runner=run) == ""


def test_a_playing_video_hides_the_face(linux):
    assert busy.busy_reason(runner=_runner(_session("Video Wake Lock", 8))) == "video"


def test_music_alone_does_not(linux):
    """Audio takes a suspend lock (4), not an idle one: a song is not a film."""
    answers = _session("Playing audio", 4)
    assert busy.busy_reason(runner=_runner(answers)) == ""


def test_a_wayland_idle_inhibit_counts_as_video(linux):
    answers = _session("idle-inhibit", 8, app="mutter")
    assert busy.busy_reason(runner=_runner(answers)) == "video"


def test_do_not_disturb_hides_it(linux):
    assert busy.busy_reason(runner=_runner({"show-banners": "false\n"})) == "do-not-disturb"


def test_a_fullscreen_x11_window_hides_it(linux):
    run = _runner({
        "show-banners": "true\n",
        "-root": "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x600003\n",
        "-id": "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n",
    })
    assert busy.busy_reason(runner=run) == "fullscreen"


def test_each_signal_can_be_switched_off(linux):
    run = _runner(_session("Video Wake Lock", 8))
    assert busy.busy_reason(video=False, runner=run) == ""


def test_windows_presentation_mode_is_always_busy(monkeypatch):
    monkeypatch.setattr(busy, "IS_WINDOWS", True)
    monkeypatch.setattr(busy, "windows_reason", lambda: "presentation")
    assert busy.busy_reason(fullscreen=False) == "presentation"


# -- the link -------------------------------------------------------------


def _fake_face(tmp_path, monkeypatch) -> Path:
    """Replace the face process with one that writes its stdin to a file."""
    out = tmp_path / "face-stdin.jsonl"
    script = f"import sys; open({str(out)!r}, 'w').write(sys.stdin.read())"
    real_popen = link.subprocess.Popen

    def popen(argv, **kwargs):
        assert argv[1:4] == ["-m", "ev.face", "--stdin"]
        return real_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(link.subprocess, "Popen", popen)
    monkeypatch.setattr(link.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv("DISPLAY", ":99")
    return out


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_link_sends_states_once_and_quits_cleanly(tmp_path, monkeypatch):
    out = _fake_face(tmp_path, monkeypatch)
    face = FaceLink()
    assert face.start()
    face.show(True)
    face.show(True)            # repeat dropped
    face.event("listening")
    face.event("listening")    # repeat dropped
    face.heard("EV, find me firefox")
    face.event("success")
    face.event("success")      # a second smile is a second smile
    face.said("Firefox is up.")
    face.close()
    assert _lines(out) == [
        {"show": True},
        {"event": "listening"},
        {"heard": "EV, find me firefox"},
        {"event": "success"},
        {"event": "success"},
        {"say": "Firefox is up."},
        {"quit": True},
    ]


def test_a_link_never_started_is_a_silent_no_op():
    face = FaceLink()
    face.event("thinking")
    face.heard("hello")
    face.show(False)
    face.close()
    assert not face.alive


def test_without_pyside_the_face_is_simply_absent(monkeypatch):
    monkeypatch.setattr(link.importlib.util, "find_spec", lambda name: None)
    assert FaceLink().start() is False


def test_a_face_that_died_costs_nothing(tmp_path, monkeypatch):
    real_popen = link.subprocess.Popen
    monkeypatch.setattr(link.subprocess, "Popen",
                        lambda argv, **kw: real_popen([sys.executable, "-c", "pass"], **kw))
    monkeypatch.setattr(link.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv("DISPLAY", ":99")
    face = FaceLink()
    face.start()
    face._process.wait(timeout=5)
    face.event("thinking")     # must not raise on a dead pipe
    face.said("still here")
    assert face._process is None


# -- the core -----------------------------------------------------------------


class RecordingFace:
    def __init__(self) -> None:
        self.log: list[tuple[str, object]] = []

    def event(self, name):
        self.log.append(("event", name))

    def show(self, on):
        self.log.append(("show", on))

    def heard(self, text):
        self.log.append(("heard", text))

    def said(self, text):
        self.log.append(("said", text))

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


def _assistant(heard: str, engaged: bool = False) -> ev_core.EV:
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = QuietUI()
    assistant.face = RecordingFace()
    assistant.speaker = FakeSpeaker()
    assistant.session = Session()
    assistant.mic = object()
    assistant.text_mode = False
    assistant._running = True
    assistant._queued_utterance = None
    assistant.routed = []
    if engaged:
        assistant.session.engage()

    async def utterance(wait_s=None):
        return Transcript(heard)

    async def route(transcript, command):
        assistant.routed.append(command)

    assistant._next_utterance = utterance
    assistant._route = route
    return assistant


def test_an_unaddressed_sentence_does_not_summon_the_face():
    assistant = _assistant("so I told him the meeting moved")
    asyncio.run(assistant._tick())
    assert assistant.face.log == []


def test_the_wake_phrase_brings_the_face_in_and_captions_it():
    assistant = _assistant("EV, find me firefox")
    asyncio.run(assistant._tick())
    log = assistant.face.log
    assert ("event", "wake") in log
    assert ("show", True) in log
    assert ("heard", "EV, find me firefox") in log
    assert assistant.routed == ["find me firefox"]


def test_mid_conversation_there_is_no_second_wake():
    assistant = _assistant("and pause the music", engaged=True)
    asyncio.run(assistant._tick())
    assert ("event", "wake") not in assistant.face.log
    assert ("heard", "and pause the music") in assistant.face.log


def test_a_reply_is_shown_as_well_as_spoken():
    assistant = _assistant("")
    assistant.memory = type("M", (), {"touch": lambda self: None})()
    asyncio.run(assistant.say("Firefox is up."))
    assert ("show", True) in assistant.face.log
    assert ("event", "speaking") in assistant.face.log
    assert ("said", "Firefox is up.") in assistant.face.log


def test_the_face_leaves_when_the_conversation_lapses():
    assistant = _assistant("")
    assistant.session.pending = None
    assistant._sync_face(listening=False)
    assert assistant.face.log[-2:] == [("show", False), ("event", "idle")]
    assistant._sync_face(listening=True)
    assert assistant.face.log[-2:] == [("show", True), ("event", "listening")]


def test_a_held_confirmation_keeps_the_face_up():
    assistant = _assistant("")
    assistant.session.pending = {"command": "delete it", "tool": "file_manager", "args": {}}
    assistant._sync_face(listening=False)
    assert ("show", True) in assistant.face.log
    assert ("event", "confirm") in assistant.face.log


def test_an_ev_built_without_init_has_a_harmless_face():
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.face.event("thinking")
    assistant.face.said("nothing")


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


def _window(**kwargs):
    from ev.face.expression import Expression, load_library
    from ev.face.window import FaceWindow

    return FaceWindow(Expression(load_library()), busy_poll_s=0, **kwargs)


def test_a_summoned_face_starts_out_of_sight(qt_app):
    window = _window(presence="summoned")
    window.start()
    assert not window.isVisible()


def test_show_brings_it_in_and_hide_takes_it_away(qt_app):
    from ev.face.window import route_line

    window = _window(presence="summoned")
    window.start()
    route_line(window, '{"show": true}')
    _pump(400)
    assert window.isVisible() and window.pos() == window._home
    route_line(window, '{"show": false}')
    _pump(400)
    assert not window.isVisible()
    assert not window._timer.isActive()  # hidden means no repaints


def test_busy_overrules_a_summons_and_lets_go_afterwards(qt_app):
    from ev.face.window import route_line

    window = _window(presence="summoned")
    window.start()
    route_line(window, '{"show": true}')
    _pump(350)
    window.set_busy("video")
    _pump(350)
    assert not window.isVisible()
    window.set_busy("")
    _pump(350)
    assert window.isVisible()


def test_captions_follow_the_face_and_never_take_input(qt_app):
    from PySide6.QtCore import Qt

    from ev.face.window import route_line

    window = _window(presence="always")
    window.start()
    route_line(window, '{"heard": "EV, what is playing"}')
    route_line(window, '{"say": "Don\'t Stop Me Now by Queen."}')
    _pump(50)
    caption = window.caption
    assert caption.isVisible()
    assert caption.windowFlags() & Qt.WindowType.WindowTransparentForInput
    # Above a face that sits at the bottom of the screen.
    assert caption.geometry().bottom() < window.geometry().top()


def test_always_mode_ignores_a_dismissal(qt_app):
    from ev.face.window import route_line

    window = _window(presence="always")
    window.start()
    route_line(window, '{"show": false}')
    _pump(350)
    assert window.isVisible()
