"""Desktop control: vision routing, the Linux overlay and kill switch, failsafes.

Offline. Vision is a fake HTTP client, the GNOME extension and X11 are fakes,
and nothing draws: the overlay is driven with a stand-in Tk root.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from ev.session import Intent, match_intent  # noqa: E402
from tools import computer_use as cu  # noqa: E402
from tools import overlay  # noqa: E402
from tools.desktop import xhud  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or "{}"
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts: list[str] = []
        self.is_closed = False

    def post(self, url, json=None, headers=None):
        self.posts.append(url)
        return self.responses.pop(0) if self.responses else FakeResponse()


def _frame():
    return cu.Frame(b"\xff\xd8jpeg", "image/jpeg", 64, 36, 1920, 1080)


def _gemini(text):
    return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _groq(text):
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def _gemini_429(delay="45s"):
    return FakeResponse(
        429,
        text='{"error":{"code":429,"details":[{"@type":"type.googleapis.com/'
        'google.rpc.RetryInfo","retryDelay":"%s"}]}}' % delay,
    )


@pytest.fixture
def auto_vision(monkeypatch):
    monkeypatch.setattr(config, "VISION_ENABLED", True)
    monkeypatch.setattr(config, "VISION_PROVIDER", "auto")
    monkeypatch.setattr(config, "VISION_FAILOVER", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(config, "GROQ_API_KEY", "q-key")
    monkeypatch.setattr(config, "GEMINI_VISION_MODEL", "gem-vision")
    monkeypatch.setattr(config, "GEMINI_MODEL_FALLBACKS", [])
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_MAX_WAIT_S", 8.0)
    monkeypatch.setattr(cu, "_resting", {})
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)


def _use(monkeypatch, client):
    monkeypatch.setattr(cu, "_vision_client", lambda: client)


# ---------------------------------------------------------------------------
# provider choice
# ---------------------------------------------------------------------------
def test_auto_prefers_gemini_when_it_has_a_key(monkeypatch):
    monkeypatch.setattr(config, "VISION_PROVIDER", "auto")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    assert config.vision_provider() == "gemini"


def test_auto_without_gemini_uses_the_brains_provider(monkeypatch):
    monkeypatch.setattr(config, "VISION_PROVIDER", "auto")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "groq")
    assert config.vision_provider() == "groq"


def test_a_named_provider_is_respected(monkeypatch):
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    assert config.vision_provider() == "groq"
    assert cu._vision_order() == ["groq"]


def test_groq_rotation_keeps_the_vision_model_when_vision_is_on_gemini(monkeypatch):
    """Vision on Gemini frees Groq's vision bucket for the brain's rotation."""
    import asyncio

    import httpx

    from ev.brain import Brain

    monkeypatch.setattr(config, "VISION_PROVIDER", "auto")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(config, "GROQ_MODEL", "a")
    monkeypatch.setattr(config, "GROQ_MODEL_ROTATION", [])
    monkeypatch.setattr(config, "GROQ_MODEL_FALLBACKS", ["v"])
    monkeypatch.setattr(config, "GROQ_VISION_MODEL", "v")

    async def go():
        client = httpx.AsyncClient()
        brain = Brain(client)
        brain._build_rotation({"a", "v"})
        await client.aclose()
        return brain._groq_rotation

    assert asyncio.run(go()) == ["a", "v"]


# ---------------------------------------------------------------------------
# failover and wait-outs
# ---------------------------------------------------------------------------
def test_gemini_rate_limit_hands_the_frame_to_groq(monkeypatch, auto_vision):
    client = FakeClient([_gemini_429("45s"), _groq("A terminal.")])
    _use(monkeypatch, client)
    assert cu.ask_vision(_frame(), "what is this") == "A terminal."
    assert ":generateContent" in client.posts[0]
    assert client.posts[1].endswith("/chat/completions")


def test_a_rested_provider_is_skipped_until_its_retry_time(monkeypatch, auto_vision):
    client = FakeClient([_gemini_429("45s"), _groq("one"), _groq("two")])
    _use(monkeypatch, client)
    cu.ask_vision(_frame(), "q")
    assert cu.ask_vision(_frame(), "q") == "two"
    assert len(client.posts) == 3, "Gemini not asked again while resting"
    assert cu._resting["gemini"] > time.monotonic() + 30


def test_a_short_gemini_wait_is_waited_out_from_the_body(monkeypatch, auto_vision):
    slept: list[float] = []
    monkeypatch.setattr(cu.time, "sleep", slept.append)
    client = FakeClient([_gemini_429("2s"), _gemini("A dialog.")])
    _use(monkeypatch, client)
    assert cu.ask_vision(_frame(), "q") == "A dialog."
    assert slept == [2.0]


def test_a_named_provider_does_not_fail_over(monkeypatch, auto_vision):
    monkeypatch.setattr(config, "VISION_PROVIDER", "gemini")
    client = FakeClient([_gemini_429("45s"), _groq("never")])
    _use(monkeypatch, client)
    with pytest.raises(cu.VisionError):
        cu.ask_vision(_frame(), "q")
    assert len(client.posts) == 1


def test_a_request_problem_does_not_fail_over(monkeypatch, auto_vision):
    """A 400 is about what was sent; the other provider would refuse it too."""
    client = FakeClient([FakeResponse(400, text="bad image"), _groq("never")])
    _use(monkeypatch, client)
    with pytest.raises(cu.VisionError):
        cu.ask_vision(_frame(), "q")
    assert len(client.posts) == 1


def test_gemini_vision_walks_its_model_ladder(monkeypatch, auto_vision):
    monkeypatch.setattr(config, "GEMINI_VISION_MODEL", "gone")
    monkeypatch.setattr(config, "GEMINI_MODEL_FALLBACKS", ["gone", "alive"])
    client = FakeClient([FakeResponse(404, text="no longer available"), _gemini("ok")])
    _use(monkeypatch, client)
    assert cu.ask_vision(_frame(), "q") == "ok"
    assert "/alive:" in client.posts[1]
    assert config.GEMINI_VISION_MODEL == "alive"


def test_a_stale_groq_budget_is_dropped_after_gemini_answers(monkeypatch, auto_vision):
    monkeypatch.setattr(cu, "_budget_remaining", 300)
    _use(monkeypatch, FakeClient([_gemini("ok")]))
    cu.ask_vision(_frame(), "q")
    assert cu.vision_budget() is None


def test_vision_failures_stay_speakable(monkeypatch, auto_vision):
    from ev.tts import clean_for_speech

    _use(monkeypatch, FakeClient([_gemini_429("90s"), FakeResponse(503, text="down")]))
    with pytest.raises(cu.VisionError) as caught:
        cu.ask_vision(_frame(), "q")
    assert "{" not in str(caught.value)
    assert clean_for_speech(str(caught.value)) == str(caught.value)


# ---------------------------------------------------------------------------
# perception failsafes that must not regress
# ---------------------------------------------------------------------------
def test_coordinates_are_fractions_of_the_real_screen():
    assert cu._coordinate("0.5", 1920) == 960
    assert cu._coordinate("1.02", 1920) is not None
    assert cu._coordinate("1.02", 1920) >= 1900, "an overshoot is not pixel 1"


def test_the_stall_detector_ignores_a_blinking_caret():
    from PIL import Image, ImageDraw

    base = Image.new("RGB", (480, 270), "white")
    ImageDraw.Draw(base).rectangle([40, 40, 300, 200], fill="navy")
    caret = base.copy()
    ImageDraw.Draw(caret).line([320, 100, 320, 112], fill="black")
    menu = base.copy()
    ImageDraw.Draw(menu).rectangle([320, 20, 470, 260], fill="black")
    a, b, c = (cu._fingerprint(i) for i in (base, caret, menu))
    assert cu.frames_match(a, b)
    assert not cu.frames_match(a, c)


def test_the_ruler_is_drawn_without_resizing_the_frame():
    from PIL import Image

    frame = Image.new("RGB", (640, 360), "white")
    ruled = cu._draw_ruler(frame, (0.0, 0.0, 1.0, 1.0))
    assert ruled.size == frame.size
    assert ruled.tobytes() != frame.tobytes()


@pytest.mark.parametrize("phrase", ["stop everything", "hands off", "freeze", "lockdown"])
def test_lockdown_phrases_are_matched_locally(phrase):
    assert match_intent(phrase) == Intent.LOCKDOWN


def test_lock_the_door_is_not_a_lockdown():
    assert match_intent("lock the door") != Intent.LOCKDOWN


# ---------------------------------------------------------------------------
# Linux hotkey parsing and the frame cut
# ---------------------------------------------------------------------------
def test_hotkeys_parse_into_x11_and_gnome_forms():
    mask, key, accel = xhud.parse_linux_hotkey("ctrl+alt+q")
    assert (mask, key, accel) == (4 | 8, "q", "<Control><Alt>q")
    assert xhud.gnome_accelerator("super+shift+f12") == "<Super><Shift>F12"
    assert xhud.gnome_accelerator("ctrl+esc") == "<Control>Escape"


@pytest.mark.parametrize("combo", ["q", "", "ctrl+", "hyper+q", "ctrl+alt+&&"])
def test_a_bare_or_unknown_hotkey_is_refused(combo):
    assert xhud.parse_linux_hotkey(combo) is None


def test_the_frame_cut_is_only_the_edges():
    w, h = 1920, 1080
    rects = xhud.frame_rects(w, h, band=2, arm=72, thick=4)
    for x, y, rw, rh in rects:
        assert 0 <= x and 0 <= y and x + rw <= w and y + rh <= h
    area = sum(rw * rh for _x, _y, rw, rh in rects)
    assert area < 0.02 * w * h, "the middle of the screen must stay uncovered"


def test_window_ids_tolerate_a_widget_with_nothing_behind_it():
    class Widget:
        def wm_frame(self):
            raise RuntimeError("no window")

        def winfo_id(self):
            return 0

    assert xhud.window_ids(Widget()) == []


# ---------------------------------------------------------------------------
# Linux kill switch routes
# ---------------------------------------------------------------------------
@pytest.fixture
def linux(monkeypatch):
    from tools.desktop import gnome, system

    monkeypatch.setattr(overlay, "IS_LINUX", True)
    monkeypatch.setattr(overlay, "IS_WINDOWS", False)
    monkeypatch.setattr(config, "AGENT_HOTKEY_ENABLED", True)
    monkeypatch.setattr(system, "is_wayland", lambda: True)
    monkeypatch.setattr(system, "desktop", lambda: "gnome")
    state = {"count": 0, "grabbed": [], "released": 0}
    monkeypatch.setattr(gnome, "available", lambda: True)
    monkeypatch.setattr(gnome, "grab_kill", lambda accel: state["grabbed"].append(accel) or True)
    monkeypatch.setattr(gnome, "kill_count", lambda: state["count"])

    def release():
        state["released"] += 1

    monkeypatch.setattr(gnome, "release_kill", release)
    return state


def test_gnome_wayland_uses_the_compositor_grab_and_fires(linux):
    fired = threading.Event()
    switch = overlay.KillSwitch("ctrl+alt+q", fired.set)
    assert switch.start() and switch.route == "gnome"
    assert linux["grabbed"] == ["<Control><Alt>q"]
    time.sleep(0.2)
    linux["count"] += 1
    assert fired.wait(2.0)
    switch.stop()
    assert linux["released"] == 1


def test_an_old_extension_falls_back_to_x11(linux, monkeypatch):
    from tools.desktop import gnome

    monkeypatch.setattr(gnome, "grab_kill", lambda accel: False)

    class Grab:
        def __init__(self, combo, on_fire):
            self.stopped = False

        def start(self):
            return True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(xhud, "X11KillGrab", Grab)
    switch = overlay.KillSwitch("ctrl+alt+q", lambda: None)
    assert switch.start() and switch.route == "x11-wayland"
    grab = switch._x11
    switch.stop()
    assert grab.stopped


def test_no_hotkey_means_the_panel_does_not_promise_one(monkeypatch):
    monkeypatch.setattr(config, "AGENT_HOTKEY_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_KILL_HOTKEY", "ctrl+alt+q")
    hud = overlay.Overlay("goal")
    hud.hotkey_live = False
    assert "CTRL+ALT+Q" not in hud._kill_hint()
    assert "stop everything" in hud._kill_hint()
    hud.hotkey_live = True
    assert "CTRL+ALT+Q" in hud._kill_hint()


# ---------------------------------------------------------------------------
# overlay teardown
# ---------------------------------------------------------------------------
def test_tk_objects_are_released_on_the_tk_thread(monkeypatch):
    """Freed on another thread, Tcl aborts the whole process (exit 134)."""
    seen: dict[str, object] = {}

    class Root:
        def mainloop(self):
            seen["loop_thread"] = threading.current_thread().name

        def destroy(self):
            seen["destroyed"] = True

    hud = overlay.Overlay("goal")

    def build():
        hud._glow_canvas = object()
        hud._badge_canvas = object()
        return Root()

    monkeypatch.setattr(hud, "_build", build)
    thread = threading.Thread(target=hud._run, name="ev-overlay-test")
    thread.start()
    thread.join(2.0)
    assert seen.get("destroyed") and seen["loop_thread"] == "ev-overlay-test"
    assert hud._glow_canvas is None and hud._badge_canvas is None


def test_glow_on_linux_without_shape_is_not_drawn(monkeypatch):
    """A full-screen sheet that eats clicks would stop the run it announces."""
    monkeypatch.setattr(overlay, "IS_LINUX", True)
    monkeypatch.setattr(xhud, "shape_window", lambda *a, **k: False)

    destroyed = []

    class FakeCanvas:
        def __init__(self, *a, **k):
            pass

        def pack(self, **k):
            pass

        def create_rectangle(self, *a, **k):
            return 1

        def create_line(self, *a, **k):
            return 2

    class FakeTop:
        def __init__(self, root):
            pass

        def overrideredirect(self, flag):
            pass

        def attributes(self, name, value=None):
            if name == "-transparentcolor":
                raise RuntimeError("bad attribute")

        def configure(self, **k):
            pass

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def geometry(self, spec):
            pass

        def update_idletasks(self):
            pass

        def destroy(self):
            destroyed.append(True)

    class TkModule:
        Toplevel = FakeTop
        Canvas = FakeCanvas

    hud = overlay.Overlay("goal")
    hud.accent = "#ff3b30"
    assert hud._build_glow(TkModule, object()) is None
    assert destroyed == [True]
