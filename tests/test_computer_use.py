"""Offline tests for screen perception and autonomous computer use.

Nothing here touches the real screen, the real mouse or the network. The
pyautogui backend is replaced by a recorder, `capture_screen` by a canned
frame, and `ask_vision` by a scripted reply, so the whole loop can be driven
end to end on a machine with no display attached.

The point of most of these is the gate rather than the plumbing: a tool that
drives the pointer with no undo is only acceptable while the confirmation
path provably holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Keep the tests deterministic regardless of the developer's own .env.
os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import config  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools import CANCELLABLE, REGISTRY, dispatch  # noqa: E402
from tools import _ALLOWED_ARGS  # noqa: E402
from tools.base import CancelToken  # noqa: E402
from tools.base import ToolResult  # noqa: E402
from tools.computer_use import (  # noqa: E402
    Frame,
    _coordinate,
    _parse_step,
    _split_keys,
    _step_actions,
    frames_match,
    keyboard_action,
    mouse_action,
    screen_task,
    take_screenshot,
)
from tools.safety import Risk, classify_gui  # noqa: E402
from tools.schemas import TOOL_SPECS, to_gemini_tools, to_openai_tools  # noqa: E402

COMPUTER_TOOLS = (
    "take_screenshot",
    "mouse_action",
    "keyboard_action",
    "screen_task",
    "browser_task",
)


# ---------------------------------------------------------------------------
# A fake hand, so no test ever moves the developer's pointer
# ---------------------------------------------------------------------------
class FakeGui:
    """Records what would have been done instead of doing it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.FAILSAFE = False
        self.PAUSE = 0

    def moveTo(self, x, y, duration=0.0):
        self.calls.append(("moveTo", x, y))

    def click(self, x=None, y=None, clicks=1, interval=0.0, button="left"):
        self.calls.append(("click", x, y, clicks, button))

    def dragTo(self, x, y, duration=0.0, button="left"):
        self.calls.append(("dragTo", x, y))

    def scroll(self, clicks):
        self.calls.append(("scroll", clicks))

    def write(self, text, interval=0.0):
        self.calls.append(("write", text))

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))

    def size(self):
        return (1920, 1080)


def _arm(monkeypatch, gui: FakeGui | None = None) -> FakeGui:
    """Point the computer-use module at a fake screen and a fake hand."""
    from tools import computer_use

    gui = gui or FakeGui()
    monkeypatch.setattr(computer_use, "_gui", lambda: gui)
    monkeypatch.setattr(computer_use, "screen_size", lambda: (1920, 1080))
    return gui


def _frame(*_args, **kwargs) -> Frame:
    """A canned frame that accepts whatever `capture_screen` is called with.

    The signature is loose on purpose: this stands in for `capture_screen`,
    which takes a region, a size, a quality and a grid flag, and a stub that
    pinned those would fail the day one of them was added rather than the
    day the behaviour changed.

    The fingerprint varies with the region so that two consecutive looks at
    the "same" screen compare equal, which is what the stall detector reads.
    """
    region = kwargs.get("region")
    return Frame(
        data=b"not-a-real-jpeg",
        media_type="image/jpeg",
        width=1280,
        height=720,
        screen_width=1920,
        screen_height=1080,
        region=region,
        fingerprint="01" * 72,
    )


def _changing_frames():
    """A `capture_screen` stand-in whose screen is different every time.

    The loop treats an unchanged screen as a stalled one, so a test that
    drives several steps has to move the picture or it trips the stall guard
    rather than the behaviour under test.
    """
    counter = {"n": 0}

    def _capture(*_args, **kwargs) -> Frame:
        counter["n"] += 1
        frame = _frame(**kwargs)
        # Inverted every frame, so each one differs from the one before it in
        # every position. Counting in binary would not do: consecutive
        # integers differ by a bit or two, which is inside the tolerance that
        # stops a blinking cursor reading as a change.
        bits = ("01" if counter["n"] % 2 else "10") * 72
        return Frame(
            data=frame.data,
            media_type=frame.media_type,
            width=frame.width,
            height=frame.height,
            screen_width=frame.screen_width,
            screen_height=frame.screen_height,
            region=frame.region,
            fingerprint=bits,
        )

    return _capture


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
def test_computer_tools_are_registered_and_described_once():
    names = {spec["name"] for spec in TOOL_SPECS}
    for tool in COMPUTER_TOOLS:
        assert tool in names, f"{tool} missing from TOOL_SPECS"
        assert tool in REGISTRY, f"{tool} missing from REGISTRY"
    # One spec per tool: a duplicate would be silently shadowed in the
    # allowed-argument map and the drift would only show up at runtime.
    assert len(names) == len(TOOL_SPECS)


def test_computer_schemas_translate_to_both_providers():
    openai_names = {tool["function"]["name"] for tool in to_openai_tools()}
    gemini_names = {
        decl["name"] for decl in to_gemini_tools()[0]["functionDeclarations"]
    }
    for tool in COMPUTER_TOOLS:
        assert tool in openai_names
        assert tool in gemini_names


def test_gemini_schema_has_no_unsupported_keys():
    """Gemini rejects any schema key outside its OpenAPI subset."""
    allowed = {"type", "description", "properties", "required", "enum", "items", "nullable"}

    def walk(node):
        if isinstance(node, dict):
            assert set(node) <= allowed, f"unsupported keys: {set(node) - allowed}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for decl in to_gemini_tools()[0]["functionDeclarations"]:
        if decl["name"] in COMPUTER_TOOLS:
            walk(decl["parameters"])


def test_allowed_args_track_the_schema():
    """`_ALLOWED_ARGS` is derived, so the two cannot drift apart."""
    for tool in COMPUTER_TOOLS:
        spec = next(s for s in TOOL_SPECS if s["name"] == tool)
        declared = set(spec["parameters"]["properties"])
        assert declared <= _ALLOWED_ARGS[tool]


def test_confirmed_is_injected_never_declared():
    """The model must not be able to approve its own click."""
    for tool in ("mouse_action", "keyboard_action", "screen_task", "browser_task"):
        spec = next(s for s in TOOL_SPECS if s["name"] == tool)
        assert "confirmed" not in spec["parameters"]["properties"]
        assert "confirmed" in _ALLOWED_ARGS[tool]
        assert "cancel" not in _ALLOWED_ARGS[tool]


def test_autonomous_loops_are_cancellable():
    assert "screen_task" in CANCELLABLE
    assert "browser_task" in CANCELLABLE


def test_dispatch_drops_arguments_outside_the_schema(monkeypatch):
    gui = _arm(monkeypatch)
    result = dispatch(
        "mouse_action",
        {"action": "click", "x": 0.5, "y": 0.5, "label": "OK", "sudo": True},
    )
    assert result.ok
    assert ("click", 960, 540, 1, "left") in gui.calls


def test_dispatch_never_lets_the_model_set_confirmed(monkeypatch):
    """`confirmed` from the model is filtered out before the tool sees it."""
    _arm(monkeypatch)
    # dispatch filters on `_ALLOWED_ARGS`, which does contain `confirmed` -
    # so the real guarantee is that the core loop is the only caller that
    # sets it. What must hold here is that a risky action still asks when
    # the argument is absent, which is the shape every model call has.
    result = dispatch("mouse_action", {"action": "click", "x": 0.5, "y": 0.5, "label": "Place order"})
    assert result.needs_confirmation


# ---------------------------------------------------------------------------
# Safety classification
# ---------------------------------------------------------------------------
def test_classify_gui_flags_spending_sending_and_deleting():
    for text in (
        "click Place order",
        "click Buy now",
        "click Proceed to checkout",
        "click Send",
        "click Submit application",
        "click Delete account",
        "type my password",
        "press alt+f4",
    ):
        assert classify_gui(text).risk is Risk.REVIEW, text


def test_classify_gui_leaves_ordinary_clicks_alone():
    for text in (
        "click the File menu",
        "click Add to cart",
        "click Next page",
        "scroll down the results",
        "click the Settings gear",
        "type wireless mouse",
        "move the slider to the right",
        "click Search",
    ):
        assert classify_gui(text).risk is Risk.SAFE, text


def test_classify_gui_is_empty_safe():
    assert classify_gui("").risk is Risk.SAFE
    assert classify_gui("   ").risk is Risk.SAFE


def test_risky_click_is_held_for_a_yes(monkeypatch):
    gui = _arm(monkeypatch)
    result = mouse_action(action="click", x="0.8", y="0.9", label="Place order")
    assert result.needs_confirmation
    assert not result.ok
    # Nothing moved. A held action that already clicked is not a gate.
    assert gui.calls == []
    # The data echoes the call back so the core loop can replay it verbatim.
    assert result.data["action"] == "click"
    assert result.data["label"] == "Place order"


def test_confirmed_risky_click_goes_through(monkeypatch):
    gui = _arm(monkeypatch)
    result = mouse_action(
        action="click", x="0.8", y="0.9", label="Place order", confirmed=True
    )
    assert result.ok
    assert ("click", 1536, 972, 1, "left") in gui.calls


def test_typing_a_blocked_command_is_refused_outright(monkeypatch):
    """A shell command typed into a terminal is still a shell command."""
    gui = _arm(monkeypatch)
    result = keyboard_action(action="type", text="format c: /q", label="the terminal")
    assert not result.ok
    assert not result.needs_confirmation  # blocked, not held
    assert gui.calls == []


def test_blocked_command_stays_blocked_even_when_confirmed(monkeypatch):
    gui = _arm(monkeypatch)
    result = keyboard_action(
        action="type", text="vssadmin delete shadows /all", confirmed=True
    )
    assert not result.ok
    assert gui.calls == []


def test_ordinary_typing_is_not_gated(monkeypatch):
    gui = _arm(monkeypatch)
    result = keyboard_action(action="type", text="wireless mouse", label="the search box")
    assert result.ok
    assert ("write", "wireless mouse") in gui.calls


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------
def test_fractions_become_pixels():
    assert _coordinate(0.0, 1920) == 0
    assert _coordinate(0.5, 1920) == 960
    assert _coordinate(1.0, 1920) == 1919  # clamped to the last real pixel
    assert _coordinate("0.25", 1080) == 270
    assert _coordinate("50%", 1920) == 960


def test_pixels_are_accepted_as_pixels():
    """A model that answers in pixels anyway should still be obeyed."""
    assert _coordinate(960, 1920) == 960
    assert _coordinate("1500", 1920) == 1500


def test_out_of_range_coordinates_are_clamped_not_refused():
    assert _coordinate(1.02, 1920) == 1919
    assert _coordinate(-0.5, 1920) == 0
    assert _coordinate(99999, 1080) == 1079


def test_missing_coordinates_are_refused_not_guessed():
    assert _coordinate(None, 1920) is None
    assert _coordinate("", 1920) is None
    assert _coordinate("somewhere", 1920) is None
    assert _coordinate(True, 1920) is None


def test_click_without_coordinates_asks_instead_of_clicking(monkeypatch):
    gui = _arm(monkeypatch)
    result = mouse_action(action="click", label="the Save button")
    assert not result.ok
    assert gui.calls == []
    assert "screenshot" in result.detail.lower()


def test_drag_needs_both_ends(monkeypatch):
    gui = _arm(monkeypatch)
    result = mouse_action(action="drag", x="0.1", y="0.1", label="the file")
    assert not result.ok
    assert gui.calls == []


def test_drag_moves_between_two_points(monkeypatch):
    gui = _arm(monkeypatch)
    result = mouse_action(
        action="drag", x="0.1", y="0.2", to_x="0.6", to_y="0.7", label="the file"
    )
    assert result.ok
    assert ("moveTo", 192, 216) in gui.calls
    assert ("dragTo", 1152, 756) in gui.calls


def test_scroll_defaults_to_downwards(monkeypatch):
    gui = _arm(monkeypatch)
    result = mouse_action(action="scroll", label="the results")
    assert result.ok
    assert any(call[0] == "scroll" and call[1] < 0 for call in gui.calls)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
def test_key_combinations_are_split_and_aliased():
    assert _split_keys("ctrl+s") == ["ctrl", "s"]
    assert _split_keys("Ctrl + Shift + T") == ["ctrl", "shift", "t"]
    assert _split_keys("return") == ["enter"]
    assert _split_keys("control alt delete") == ["ctrl", "alt", "delete"]
    assert _split_keys("") == []


def test_single_key_presses_and_combinations_take_different_paths(monkeypatch):
    gui = _arm(monkeypatch)
    keyboard_action(action="press", keys="enter")
    keyboard_action(action="press", keys="ctrl+s")
    assert ("press", "enter") in gui.calls
    assert ("hotkey", ("ctrl", "s")) in gui.calls


def test_alt_f4_is_held_for_a_yes(monkeypatch):
    gui = _arm(monkeypatch)
    result = keyboard_action(action="press", keys="alt+f4", label="close the window")
    assert result.needs_confirmation
    assert gui.calls == []


# ---------------------------------------------------------------------------
# take_screenshot
# ---------------------------------------------------------------------------
def test_screenshot_speaks_the_description_and_keeps_detail_separate(monkeypatch):
    from tools import computer_use

    monkeypatch.setattr(computer_use, "capture_screen", _frame)
    monkeypatch.setattr(
        computer_use, "ask_vision", lambda frame, prompt, system="": "VS Code, three tabs."
    )
    result = take_screenshot(question="what's open")
    assert result.ok
    assert result.speech == "VS Code, three tabs."
    # The machine detail - resolution, what was asked - is for the model, not
    # the speaker. Speech and detail are separate channels.
    assert "1920x1080" in result.detail
    assert "1920x1080" not in result.speech


def test_screenshot_reports_a_vision_failure_in_english(monkeypatch):
    from tools import computer_use

    def boom(frame, prompt, system=""):
        raise computer_use.VisionError("Looking at the screen timed out.")

    monkeypatch.setattr(computer_use, "capture_screen", _frame)
    monkeypatch.setattr(computer_use, "ask_vision", boom)
    result = take_screenshot()
    assert not result.ok
    # This string reaches a speech synthesiser. No JSON, no status codes.
    assert "{" not in result.speech
    assert result.speech == clean_for_speech(result.speech)


def test_screenshot_save_path_goes_through_the_root_check(monkeypatch, tmp_path):
    """A saved frame obeys FILE_ROOTS like every other file E.V. writes."""
    from tools import computer_use

    monkeypatch.setattr(computer_use, "capture_screen", _frame)
    monkeypatch.setattr(computer_use, "ask_vision", lambda *a, **k: "A screen.")
    # `file_manager` reads these off the config module at call time, so
    # redirecting them here redirects the root check too - which is the
    # whole point: the suite can never touch a real user directory.
    monkeypatch.setattr(config, "FILE_ROOTS", [tmp_path])
    monkeypatch.setattr(config, "FILE_DEFAULT_DIR", tmp_path)

    result = take_screenshot(save_as="shot")
    assert result.ok
    assert (tmp_path / "shot.jpg").read_bytes() == b"not-a-real-jpeg"

    # Outside the roots: refused, and nothing written.
    refused = take_screenshot(save_as="C:/Windows/System32/shot.png")
    assert not refused.ok
    assert "{" not in refused.speech


def test_screenshot_without_a_capture_backend_says_so(monkeypatch):
    from tools import computer_use

    def no_backend(*_args, **_kwargs):
        raise computer_use.CaptureError("mss not installed")

    monkeypatch.setattr(computer_use, "capture_screen", no_backend)
    result = take_screenshot()
    assert not result.ok
    assert "mss" in result.detail


# ---------------------------------------------------------------------------
# The vision request, on the wire
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or "{}"
        # Real responses carry these, and the 429 path reads them to work out
        # how long to wait. Defaulting to empty means "the server said
        # nothing", which is the case that gives up rather than sleeping.
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeClient:
    """Captures what would have been posted instead of posting it."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts: list[tuple[str, dict]] = []
        self.is_closed = False

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        return self.responses.pop(0) if self.responses else FakeResponse()


def _groq_reply(text):
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def test_groq_vision_request_carries_the_frame_as_a_data_url(monkeypatch):
    from tools import computer_use

    client = FakeClient([_groq_reply("A terminal.")])
    monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")

    answer = computer_use.ask_vision(_frame(), "what is this", "be brief")
    assert answer == "A terminal."

    url, payload = client.posts[0]
    assert url.endswith("/chat/completions")
    assert payload["messages"][0]["role"] == "system"
    parts = payload["messages"][1]["content"]
    assert parts[0]["text"] == "what is this"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_gemini_vision_request_carries_the_frame_inline(monkeypatch):
    from tools import computer_use

    client = FakeClient(
        [FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "A terminal."}]}}]})]
    )
    monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
    monkeypatch.setattr(config, "VISION_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")

    answer = computer_use.ask_vision(_frame(), "what is this")
    assert answer == "A terminal."

    url, payload = client.posts[0]
    assert ":generateContent" in url
    parts = payload["contents"][0]["parts"]
    assert parts[0]["text"] == "what is this"
    assert parts[1]["inline_data"]["mime_type"] == "image/jpeg"


def test_a_missing_model_walks_down_the_fallback_ladder(monkeypatch):
    """Groq's vision catalogue differs per account, so 404 is not the end."""
    from tools import computer_use

    client = FakeClient(
        [
            FakeResponse(404, text='{"error":{"code":"model_not_found"}}'),
            _groq_reply("A terminal."),
        ]
    )
    monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_VISION_MODEL", "does-not-exist")
    monkeypatch.setattr(config, "GROQ_VISION_FALLBACKS", ["works-fine"])

    assert computer_use.ask_vision(_frame(), "what is this") == "A terminal."
    assert [payload["model"] for _, payload in client.posts] == [
        "does-not-exist",
        "works-fine",
    ]
    # The working rung sticks, so the next screenshot does not pay for the
    # failed one all over again.
    assert config.GROQ_VISION_MODEL == "works-fine"


def test_a_decommissioned_model_also_advances_the_ladder(monkeypatch):
    """Groq says 404 for a model that never existed and 400 for a retired one."""
    from tools import computer_use

    client = FakeClient(
        [
            FakeResponse(
                400,
                text='{"error":{"message":"has been decommissioned and is no longer supported"}}',
            ),
            _groq_reply("A terminal."),
        ]
    )
    monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_VISION_MODEL", "retired-model")
    monkeypatch.setattr(config, "GROQ_VISION_FALLBACKS", ["works-fine"])

    assert computer_use.ask_vision(_frame(), "what is this") == "A terminal."


def test_a_bad_key_does_not_walk_the_ladder(monkeypatch):
    """A rejected key is about the request, not the model. Retrying wastes time."""
    from tools import computer_use
    import pytest

    client = FakeClient([FakeResponse(401, text="unauthorized")])
    monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_VISION_FALLBACKS", ["a", "b", "c"])

    with pytest.raises(computer_use.VisionError):
        computer_use.ask_vision(_frame(), "what is this")
    assert len(client.posts) == 1


def test_vision_waits_out_a_short_rate_limit_instead_of_failing(monkeypatch):
    """A 429 half way through a screen task must not throw the task away.

    A screen task is a dozen requests against one per-minute budget, so it
    is the most likely thing to meet a rate limit and the worst thing to
    lose to one: several steps of real work have already happened and the
    desktop is mid-way through a job nobody asked to abandon.
    """
    from tools import computer_use

    slept: list[float] = []
    client = FakeClient(
        [
            FakeResponse(429, text="slow down", headers={"retry-after": "1.5"}),
            FakeResponse(200, payload={"choices": [{"message": {"content": "A window."}}]}),
        ]
    )
    monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
    monkeypatch.setattr(computer_use.time, "sleep", slept.append)
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")

    assert computer_use.ask_vision(_frame(), "what is this") == "A window."
    assert slept == [1.5], "should wait exactly as long as the server asked"


def test_vision_gives_up_on_a_rate_limit_nobody_would_wait_out(monkeypatch):
    """Nobody stands at a microphone for three minutes."""
    from tools import computer_use
    import pytest

    slept: list[float] = []
    client = FakeClient(
        [FakeResponse(429, text="slow down", headers={"retry-after": "600"})]
    )
    monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
    monkeypatch.setattr(computer_use.time, "sleep", slept.append)
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")

    with pytest.raises(computer_use.VisionError):
        computer_use.ask_vision(_frame(), "what is this")
    assert slept == []


def test_vision_errors_are_english_not_json(monkeypatch):
    """These strings can reach a speech synthesiser."""
    from tools import computer_use
    import pytest

    for status, body in ((401, "unauthorized"), (429, "slow down"), (500, "boom")):
        client = FakeClient([FakeResponse(status, text=body)])
        monkeypatch.setattr(computer_use, "_vision_client", lambda: client)
        monkeypatch.setattr(config, "VISION_PROVIDER", "groq")
        with pytest.raises(computer_use.VisionError) as caught:
            computer_use.ask_vision(_frame(), "what is this")
        message = str(caught.value)
        assert "{" not in message
        assert message == clean_for_speech(message)


# ---------------------------------------------------------------------------
# The vision loop
# ---------------------------------------------------------------------------
def _script(monkeypatch, replies: list[str], moving: bool = True) -> list[str]:
    """Feed `screen_task` a fixed sequence of model replies.

    `moving` gives every step a different screen. That is the normal case -
    an action that does something changes the picture - and a test that left
    the frame identical would trip the stall guard instead of exercising
    whatever it meant to.
    """
    from tools import computer_use

    seen: list[str] = []
    monkeypatch.setattr(
        computer_use, "capture_screen", _changing_frames() if moving else _frame
    )
    monkeypatch.setattr(config, "COMPUTER_ACTION_PAUSE_S", 0.0)

    def fake_vision(frame, prompt, system=""):
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(computer_use, "ask_vision", fake_vision)
    return seen


def test_parse_step_survives_fences_and_prose():
    assert _parse_step('{"action": "done"}')["action"] == "done"
    assert _parse_step('```json\n{"action": "click", "x": 0.5}\n```')["action"] == "click"
    assert _parse_step('Sure! {"action": "fail"} hope that helps')["action"] == "fail"
    assert _parse_step("no json here at all") == {}
    assert _parse_step("[1, 2, 3]") == {}


def test_loop_acts_then_looks_again_then_finishes(monkeypatch):
    gui = _arm(monkeypatch)
    prompts = _script(
        monkeypatch,
        [
            '{"action": "click", "x": 0.5, "y": 0.5, "label": "the Start button"}',
            '{"action": "done", "speech": "Settings are open."}',
        ],
    )
    result = screen_task(task="open settings")
    assert result.ok
    assert result.speech == "Settings are open."
    assert ("click", 960, 540, 1, "left") in gui.calls
    # Two looks, not one: the second is what verifies the first worked.
    assert len(prompts) == 2
    assert "Step 2" in prompts[1]
    assert "the Start button" in prompts[1]


def test_loop_stops_at_the_step_ceiling(monkeypatch):
    _arm(monkeypatch)
    _script(monkeypatch, ['{"action": "scroll", "amount": -400, "label": "the page"}'])
    result = screen_task(task="scroll about a bit", max_steps="3")
    assert result.ok
    assert "ceiling" in result.detail


def test_loop_reports_a_model_give_up_as_a_failure(monkeypatch):
    _arm(monkeypatch)
    _script(monkeypatch, ['{"action": "fail", "speech": "No settings window here."}'])
    result = screen_task(task="open settings")
    assert not result.ok
    assert result.speech == "No settings window here."


def test_loop_holds_a_risky_step_mid_run(monkeypatch):
    """A harmless goal that reaches a Delete button stops and asks."""
    gui = _arm(monkeypatch)
    _script(
        monkeypatch,
        ['{"action": "click", "x": 0.5, "y": 0.5, "label": "Delete all messages"}'],
    )
    result = screen_task(task="tidy up my inbox")
    assert result.needs_confirmation
    assert gui.calls == []
    # Confirming re-runs the same goal, which is safe because the loop
    # re-reads the screen rather than replaying what it already did.
    assert result.data["task"] == "tidy up my inbox"


def test_loop_holds_a_risky_goal_before_the_first_look(monkeypatch):
    gui = _arm(monkeypatch)
    seen = _script(monkeypatch, ['{"action": "done"}'])
    result = screen_task(task="buy the first result")
    assert result.needs_confirmation
    assert gui.calls == []
    assert seen == []  # not even a screenshot was taken


def test_loop_honours_a_cancel_between_steps(monkeypatch):
    gui = _arm(monkeypatch)
    _script(monkeypatch, ['{"action": "click", "x": 0.5, "y": 0.5, "label": "next"}'])
    token = CancelToken()
    token.cancel()
    result = screen_task(task="page through the results", cancel=token)
    # A cancelled run is a success carrying `cancelled`: whatever ran, ran.
    assert result.ok
    assert result.cancelled
    assert gui.calls == []


def test_loop_refuses_an_empty_goal():
    result = screen_task(task="   ")
    assert not result.ok


def test_loop_says_so_when_vision_is_off(monkeypatch):
    monkeypatch.setattr(config, "VISION_ENABLED", False)
    result = screen_task(task="open settings")
    assert not result.ok
    assert "{" not in result.speech


# ---------------------------------------------------------------------------
# Speech purity
# ---------------------------------------------------------------------------
def test_computer_tool_speech_survives_the_speech_cleaner(monkeypatch):
    """Nothing these tools say may be label-shaped or markdown-flavoured."""
    _arm(monkeypatch)
    results = [
        mouse_action(action="click", x="0.5", y="0.5", label="the File menu"),
        mouse_action(action="scroll", amount="-400", label="the page"),
        keyboard_action(action="type", text="hello", label="the box"),
        keyboard_action(action="press", keys="enter"),
        mouse_action(action="click", x="0.5", y="0.5", label="Send"),
        keyboard_action(action="type", text="format c:"),
        screen_task(task=""),
    ]
    for result in results:
        assert result.speech
        assert result.speech == clean_for_speech(result.speech)
        for banned in ("Spoke:", "E.V.:", "Reply:", "**", "{", "}"):
            assert banned not in result.speech


def test_speech_and_detail_are_different_channels(monkeypatch):
    """Machine detail must never be the thing that gets said."""
    gui = _arm(monkeypatch)
    result = mouse_action(action="click", x="0.5", y="0.5", label="the File menu")
    assert result.ok
    assert result.speech == "Clicked."
    # Coordinates belong to the model's next turn, not the speaker's mouth.
    assert "960" in result.detail
    assert "960" not in result.speech
    assert gui.calls  # and it really did click


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------
def test_disabling_computer_use_stops_every_action(monkeypatch):
    gui = _arm(monkeypatch)
    monkeypatch.setattr(config, "COMPUTER_USE_ENABLED", False)
    for result in (
        mouse_action(action="click", x="0.5", y="0.5", label="anything"),
        keyboard_action(action="type", text="anything"),
        take_screenshot(),
        screen_task(task="anything"),
    ):
        assert not result.ok
    assert gui.calls == []


def test_a_missing_backend_fails_politely_rather_than_raising(monkeypatch):
    from tools import computer_use

    monkeypatch.setattr(computer_use, "_gui", lambda: None)
    monkeypatch.setattr(computer_use, "screen_size", lambda: (1920, 1080))
    result = mouse_action(action="click", x="0.5", y="0.5", label="anything")
    assert not result.ok
    assert "pyautogui" in result.detail


# ---------------------------------------------------------------------------
# Batching, and the rule that a launch ends one
# ---------------------------------------------------------------------------
def test_a_single_action_object_still_works():
    """The old one-action shape is what a model emits when there is one job."""
    actions = _step_actions({"action": "click", "x": 0.5, "y": 0.5})
    assert [a["action"] for a in actions] == ["click"]


def test_typing_then_pressing_enter_is_one_batch():
    """Neither needs a fresh frame, and paying a vision call for the gap is
    most of why a long task runs out of time."""
    actions = _step_actions(
        {"actions": [{"action": "type", "text": "hi"}, {"action": "press", "keys": "enter"}]}
    )
    assert [a["action"] for a in actions] == ["type", "press"]


def test_a_launch_ends_the_batch_so_the_next_step_looks_first():
    """The bug this exists for: "open Notepad and type hello" was planned as
    launch, wait, type - and on a machine where Notepad was already open on a
    page of the user's own notes, the "hello" landed in the middle of them.

    Launching tells you an application is running. It says nothing about what
    is in it, so the typing has to wait for a look.
    """
    actions = _step_actions(
        {
            "actions": [
                {"action": "launch", "app": "notepad"},
                {"action": "wait", "window": "Notepad"},
                {"action": "type", "text": "hello"},
            ]
        }
    )
    assert [a["action"] for a in actions] == ["launch", "wait"], (
        "a trailing wait may ride along with a launch, but typing may not"
    )


def test_focus_ends_the_batch_for_the_same_reason():
    actions = _step_actions(
        {
            "actions": [
                {"action": "focus", "window": "Visual Studio Code"},
                {"action": "type", "text": "rm -rf"},
            ]
        }
    )
    assert [a["action"] for a in actions] == ["focus"]


def test_a_batch_stops_at_done():
    """Anything planned after "done" was planned against an unseen screen."""
    actions = _step_actions(
        {"actions": [{"action": "type", "text": "hi"}, {"action": "done"}, {"action": "click"}]}
    )
    assert [a["action"] for a in actions] == ["type", "done"]


def test_a_batch_is_capped_rather_than_refused(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_TASK_MAX_BATCH", 2)
    actions = _step_actions({"actions": [{"action": "scroll"}] * 7})
    assert len(actions) == 2


def test_a_reply_with_no_action_at_all_is_empty():
    assert _step_actions({"observation": "a screen", "plan": "do something"}) == []


# ---------------------------------------------------------------------------
# Launch, focus and wait go through the tools that already have the checks
# ---------------------------------------------------------------------------
def test_launch_routes_through_open_app(monkeypatch):
    """Not through Popen directly: `open_app` is where a path-shaped argument
    meets FILE_ROOTS, and routing around it would be a way to open a folder
    the file tools would have refused."""
    from tools import app_launcher, computer_use

    seen = {}

    def fake_open_app(app="", arguments="", **_):
        seen["app"] = app
        seen["arguments"] = arguments
        return ToolResult.success("Up.", "launched")

    monkeypatch.setattr(app_launcher, "open_app", fake_open_app)
    result = computer_use._apply_step(
        {"action": "launch", "app": "notepad", "arguments": "notes.txt"}
    )
    assert result.ok
    assert seen == {"app": "notepad", "arguments": "notes.txt"}


def test_focus_that_never_arrives_is_a_failure_that_names_the_window(monkeypatch):
    """A window that would not come forward must not read as success: the
    keys would go to whatever was in front instead."""
    from tools import computer_use

    monkeypatch.setattr(computer_use.window, "focus_by_title", lambda *a, **k: False)
    result = computer_use._apply_step({"action": "focus", "window": "Ledger"})
    assert not result.ok
    assert "Ledger" in result.detail


def test_wait_polls_for_a_window_rather_than_sleeping(monkeypatch):
    from tools import computer_use

    asked: list[str] = []

    def fake_wait(title, timeout=0.0, **_):
        asked.append(title)
        return object()

    monkeypatch.setattr(computer_use.window, "wait_for_window", fake_wait)
    result = computer_use._apply_step({"action": "wait", "window": "Notepad"})
    assert result.ok
    assert asked == ["Notepad"]


# ---------------------------------------------------------------------------
# The stall guard
# ---------------------------------------------------------------------------
def test_an_unchanged_screen_twice_over_ends_the_run(monkeypatch):
    """A dead button looks exactly like the one just clicked, so without this
    the model clicks it until the step budget is gone."""
    _arm(monkeypatch)
    _script(
        monkeypatch,
        ['{"action": "click", "x": 0.5, "y": 0.5, "label": "a dead button"}'],
        moving=False,
    )
    result = screen_task(task="press the button")
    assert not result.ok
    assert "did not change" in result.detail


def test_the_model_is_told_when_nothing_happened(monkeypatch):
    _arm(monkeypatch)
    prompts = _script(
        monkeypatch,
        ['{"action": "click", "x": 0.5, "y": 0.5, "label": "a dead button"}'],
        moving=False,
    )
    screen_task(task="press the button")
    assert any("NOT changed" in prompt for prompt in prompts)


def test_a_moving_screen_is_not_treated_as_stalled(monkeypatch):
    _arm(monkeypatch)
    _script(
        monkeypatch,
        [
            '{"action": "click", "x": 0.5, "y": 0.5, "label": "one"}',
            '{"action": "click", "x": 0.5, "y": 0.5, "label": "two"}',
            '{"action": "done", "speech": "Done."}',
        ],
    )
    result = screen_task(task="click about")
    assert result.ok
    assert result.speech == "Done."


def test_fingerprints_tolerate_a_blinking_cursor_but_not_a_new_window():
    """Coarse on purpose: an exact comparison of two screenshots is always
    different, which answers a question nobody asked."""
    base = "01" * 72
    twitch = "1" + base[1:]
    assert frames_match(base, twitch)
    assert not frames_match(base, "10" * 72)
    assert not frames_match(base, "")


# ---------------------------------------------------------------------------
# The window inventory
# ---------------------------------------------------------------------------
def test_windows_are_described_as_fractions_of_the_screen():
    """The model speaks fractions everywhere else; the inventory has to agree
    or it is one more coordinate space to get wrong."""
    from tools import window as window_module
    from tools.window import WindowInfo

    fake = [
        WindowInfo(1, "Untitled - Notepad", "Notepad", 480, 270, 1440, 810, focused=True),
        WindowInfo(2, "EV - Visual Studio Code", "Chrome_WidgetWin_1", 0, 0, 1920, 1080),
    ]
    # Patch the enumeration rather than the description, so the formatting
    # under test is the real one.
    original = window_module.list_windows
    try:
        window_module.list_windows = lambda limit=12: fake
        text = window_module.describe_windows(1920, 1080)
    finally:
        window_module.list_windows = original

    assert "0.25,0.25 to 0.75,0.75" in text
    assert "[FOCUSED]" in text
    assert text.index("Notepad") < text.index("Visual Studio Code"), "front to back"


def test_the_inventory_is_harmless_when_there_is_no_window_manager():
    """Nothing here may raise: it rides in every step of every screen task."""
    from tools import computer_use

    assert isinstance(computer_use._screen_context(), str)


# ---------------------------------------------------------------------------
# Typing that pyautogui cannot do
# ---------------------------------------------------------------------------
def test_short_plain_text_is_typed_not_pasted():
    """Typing is what applications expect, and some fields refuse a paste."""
    from tools import computer_use

    assert not computer_use._needs_paste("hello")


def test_long_or_non_ascii_text_goes_through_the_clipboard():
    """pyautogui presses one key per character against the current layout, so
    a character that layout has no key for is dropped in silence."""
    from tools import computer_use

    assert computer_use._needs_paste("x" * 200)
    assert computer_use._needs_paste("café")
    assert computer_use._needs_paste("an em dash — here")


def test_pasting_restores_the_clipboard_the_user_had(monkeypatch):
    """Silently keeping someone's clipboard is a small rudeness that makes an
    assistant untrustworthy."""
    from tools import computer_use

    clipboard = {"value": "the user's own copied text"}
    writes: list[str] = []

    def fake_write(text):
        writes.append(text)
        clipboard["value"] = text
        return True

    monkeypatch.setattr(computer_use, "IS_WINDOWS", True)
    monkeypatch.setattr(computer_use, "_clipboard_read", lambda: clipboard["value"])
    monkeypatch.setattr(computer_use, "_clipboard_write", fake_write)

    gui = FakeGui()
    how = computer_use._enter_text(gui, "a" * 100)
    assert how == "pasted"
    assert any(call[0] == "hotkey" for call in gui.calls), "should paste, not type"
    assert not any(call[0] == "write" for call in gui.calls)
    assert clipboard["value"] == "the user's own copied text"
    assert writes[0] == "a" * 100, "the text really did go on the clipboard first"


def test_typing_falls_back_when_the_clipboard_refuses(monkeypatch):
    """A clipboard Windows will not hand over costs speed, not the action."""
    from tools import computer_use

    monkeypatch.setattr(computer_use, "IS_WINDOWS", True)
    monkeypatch.setattr(computer_use, "_clipboard_read", lambda: None)
    monkeypatch.setattr(computer_use, "_clipboard_write", lambda text: False)

    gui = FakeGui()
    assert computer_use._enter_text(gui, "z" * 100) == "typed"
    assert any(call[0] == "write" for call in gui.calls)


# ---------------------------------------------------------------------------
# Looking closely
# ---------------------------------------------------------------------------
def test_a_region_is_read_as_fractions_and_padded():
    """Padded outwards because a model asked to box some text boxes the text
    and not the control it sits on."""
    from tools import computer_use

    region = computer_use._parse_region("0.25,0.25,0.75,0.75")
    assert region is not None
    left, top, right, bottom = region
    assert left < 480 and top < 270 and right > 1440 and bottom > 810


def test_an_unusable_region_falls_back_to_the_whole_screen():
    """A malformed crop should cost detail, never the look itself."""
    from tools import computer_use

    assert computer_use._parse_region("nonsense") is None
    assert computer_use._parse_region("0.1,0.2") is None
    assert computer_use._parse_region("") is None


def test_a_zoom_asks_for_the_region_then_goes_back_to_the_whole_screen(monkeypatch):
    """A zoom is one look, not a mode the loop can get stuck in."""
    from tools import computer_use

    regions: list[object] = []

    def recording_capture(*_args, **kwargs):
        regions.append(kwargs.get("region"))
        return _changing_frames()(**kwargs)

    monkeypatch.setattr(config, "COMPUTER_ACTION_PAUSE_S", 0.0)
    monkeypatch.setattr(computer_use, "capture_screen", recording_capture)
    _arm(monkeypatch)

    replies = [
        '{"action": "zoom", "x": 0.1, "y": 0.1, "to_x": 0.4, "to_y": 0.4, "label": "the error"}',
        '{"action": "done", "speech": "Read it."}',
    ]
    seen: list[str] = []

    def fake_vision(frame, prompt, system=""):
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(computer_use, "ask_vision", fake_vision)
    result = screen_task(task="read the error")

    assert result.ok
    assert regions[0] is None, "the first look is the whole screen"
    assert regions[1] is not None, "the second is the region asked for"
    assert regions[2:] == [] or regions[2] is None, "and then back to the whole screen"
    assert any("ZOOM" in prompt for prompt in seen)


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------
def test_the_step_prompt_carries_the_window_list(monkeypatch):
    from tools import computer_use

    _arm(monkeypatch)
    monkeypatch.setattr(
        computer_use, "_screen_context", lambda: "1. Untitled - Notepad [FOCUSED] at 0,0 to 1,1"
    )
    prompts = _script(monkeypatch, ['{"action": "done", "speech": "Done."}'])
    screen_task(task="look at things")
    assert "Untitled - Notepad" in prompts[0]


def test_the_plan_from_the_first_reply_is_carried_forward(monkeypatch):
    """So step four still knows what step one was trying to achieve."""
    _arm(monkeypatch)
    prompts = _script(
        monkeypatch,
        [
            '{"plan": "open it then type", "action": "click", "x": 0.5, "y": 0.5, "label": "a"}',
            '{"action": "done", "speech": "Done."}',
        ],
    )
    screen_task(task="do the thing")
    assert "open it then type" in prompts[1]


def test_max_steps_counts_actions_not_looks(monkeypatch):
    """One reply may carry a batch, so bounding the loop alone would quietly
    allow several times the steps the caller asked for. `max_steps` is a
    promise about what happens to the screen."""
    _arm(monkeypatch)
    _script(
        monkeypatch,
        [
            '{"actions": [{"action": "scroll", "amount": -400, "label": "a"},'
            ' {"action": "scroll", "amount": -400, "label": "b"},'
            ' {"action": "scroll", "amount": -400, "label": "c"}]}'
        ],
    )
    result = screen_task(task="tidy the view", max_steps="4")
    assert result.ok
    assert "ceiling" in result.detail
    # Three from the first batch, then one more before the guard stops it
    # mid-batch. Without the inner check the second batch ran in full and
    # six actions happened where four were asked for.
    done = result.detail.split("Done: ")[1].split(". Ask again")[0]
    assert len(done.split("; ")) == 4
