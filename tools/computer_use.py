"""Screen perception and direct computer control - E.V.'s eyes and hands.

Four tools live here, in increasing order of how much they can do to a
machine:

* `take_screenshot` looks at the screen and answers a question about it.
* `mouse_action` moves, clicks, drags and scrolls.
* `keyboard_action` types text and sends hotkeys.
* `screen_task` runs the whole loop by itself: capture, read, decide, act,
  look again.

Four things shape the design, and all four are load-bearing.

**No local model, and no frame on disk.** The screen is grabbed into memory,
downscaled, JPEG-encoded and posted to the same provider the brain already
uses, as one ordinary JSON request. That keeps the resident cost to the peak
of a single frame - a couple of megabytes, freed immediately - instead of the
gigabytes a local vision model would want. Nothing is written to disk unless
the user explicitly asks for a saved copy, and that write goes through
`file_manager`'s root check like every other file E.V. touches.

**Every import is lazy.** `mss`, `Pillow` and `pyautogui` are loaded on first
use and not before, so a session that never looks at the screen never pays
for them. Each has a fallback and each degrades to a spoken explanation
rather than a traceback.

**Coordinates are fractions, not pixels.** The model sees a downscaled frame,
so pixel coordinates from it would be wrong by whatever the scale factor
happened to be. Fractions of the screen survive the resize, survive a
different monitor, and survive a DPI change.

**Nothing risky happens without a yes.** Every action is described in
English before it runs and that description goes through
`tools.safety.classify_gui`. Typed text goes through `classify` as well, so a
shell command typed into a terminal window meets the same blocked patterns it
would have met had it been run directly. This is not a sandbox - there is no
sandbox for a real mouse - which is exactly why the gate matters.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

import config
from tools import window
from tools.base import IS_WINDOWS, CancelToken, ToolResult, was_cancelled
from tools.safety import classify, classify_gui
from tools.overlay import taking_over

log = logging.getLogger("ev.tools.computer_use")


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    """One captured screen, encoded and ready to post.

    `width`/`height` are the encoded image's size; `screen_width`/
    `screen_height` are the real desktop. They differ whenever the frame was
    downscaled, which is most of the time, and conflating the two is exactly
    how a click lands in the wrong place.

    `region` is set when this frame is a zoom rather than the whole desktop,
    and holds the part of the screen it covers in real pixels. Nothing
    downstream has to do arithmetic with it: the ruler drawn on a zoomed
    frame is labelled in whole-screen fractions, so a coordinate read off a
    zoom means the same thing as one read off a full frame.
    """

    data: bytes
    media_type: str
    width: int
    height: int
    screen_width: int
    screen_height: int
    region: tuple[int, int, int, int] | None = None
    fingerprint: str = ""

    @property
    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """The screen fractions this frame covers: whole screen unless zoomed."""
        if not self.region or self.screen_width <= 0 or self.screen_height <= 0:
            return (0.0, 0.0, 1.0, 1.0)
        left, top, right, bottom = self.region
        return (
            left / self.screen_width,
            top / self.screen_height,
            right / self.screen_width,
            bottom / self.screen_height,
        )


class CaptureError(RuntimeError):
    """The screen could not be grabbed, with a reason worth saying aloud."""


def screen_size() -> tuple[int, int]:
    """Desktop size in real pixels.

    `ctypes` first on Windows, because it is free and needs nothing
    installed. `pyautogui` only as a fallback, since importing it pulls in
    Pillow on most installations.
    """
    if IS_WINDOWS:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)
            if width > 0 and height > 0:
                return width, height
        except Exception as exc:  # pragma: no cover - platform specific
            log.debug("GetSystemMetrics failed: %s", exc)

    try:
        import pyautogui

        size = pyautogui.size()
        return int(size[0]), int(size[1])
    except Exception as exc:
        log.debug("Could not determine screen size: %s", exc)
        return 0, 0


def _fingerprint(image: Any) -> str:
    """A 12x12 average hash of a frame, for "did anything actually happen?".

    Deliberately coarse. An exact comparison of two screenshots is always
    "different" - a blinking caret, a clock, an antialiased hover state - so
    it would answer yes to a question nobody asked. At twelve squares a menu
    opening registers and a cursor blinking does not, which is the
    distinction the loop needs.
    """
    try:
        from PIL import Image

        thumb = image.convert("L").resize((12, 12), Image.BILINEAR)
        pixels = list(thumb.getdata())
        if not pixels:
            return ""
        average = sum(pixels) / len(pixels)
        return "".join("1" if pixel > average else "0" for pixel in pixels)
    except Exception as exc:  # pragma: no cover - never worth failing a frame
        log.debug("Could not fingerprint the frame: %s", exc)
        return ""


def frames_match(first: str, second: str, tolerance: int = 2) -> bool:
    """Whether two fingerprints describe the same screen.

    The tolerance is what keeps a clock in the corner from reading as a
    change, while a dialog appearing moves far more than two squares.
    """
    if not first or not second or len(first) != len(second):
        return False
    differences = sum(1 for a, b in zip(first, second) if a != b)
    return differences <= tolerance


def _grid_font(size: int) -> Any:
    """A readable label font, falling back to Pillow's bitmap one."""
    from PIL import ImageFont

    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_ruler(image: Any, bounds: tuple[float, float, float, float]) -> Any:
    """Overlay a labelled coordinate grid, in whole-screen fractions.

    This is the cheapest accuracy there is. Asked for a fraction from a bare
    screenshot, a model estimates one by eye and lands a few percent out -
    which on a 1920px screen is tens of pixels, and tens of pixels is a
    different menu item. Given a ruler it reads the number off the nearest
    line instead, and the error collapses to half a division.

    `bounds` is the part of the screen this image covers, so the labels on a
    zoomed frame still read as whole-screen fractions. That is deliberate:
    the alternative is asking a model to rescale its own answer, which is
    exactly the arithmetic it is worst at.
    """
    from PIL import Image, ImageDraw

    canvas = image.convert("RGB")
    width, height = canvas.size
    divisions = max(2, min(20, config.VISION_GRID_DIVISIONS))
    left, top, right, bottom = bounds
    font = _grid_font(max(11, width // 110))

    # The lines go on a translucent layer of their own. A solid grid is easy
    # to read and sits directly on top of the menu entries and file names the
    # model has to read through it, so it buys coordinate accuracy by
    # spending text accuracy. Blended, it does neither.
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    lines = ImageDraw.Draw(overlay)
    for index in range(divisions + 1):
        ratio = index / divisions
        px = int(round(ratio * (width - 1)))
        py = int(round(ratio * (height - 1)))
        lines.line([(px, 0), (px, height)], fill=(255, 0, 170, 90), width=1)
        lines.line([(0, py), (width, py)], fill=(255, 0, 170, 90), width=1)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    # Labels go on solid, after the blend. A bare number over a light
    # background is invisible, and an invisible ruler is worse than none
    # because the model still believes it read one.
    draw = ImageDraw.Draw(canvas)

    def _label(text: str, x: int, y: int) -> None:
        box = draw.textbbox((x, y), text, font=font)
        draw.rectangle(
            (box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill=(20, 20, 20)
        )
        draw.text((x, y), text, fill=(255, 235, 59), font=font)

    for index in range(divisions + 1):
        ratio = index / divisions
        px = int(round(ratio * (width - 1)))
        py = int(round(ratio * (height - 1)))
        _label(f"{left + ratio * (right - left):.2f}", min(px + 3, width - 34), 2)
        _label(f"{top + ratio * (bottom - top):.2f}", 2, min(py + 2, height - 16))

    return canvas


def _encode_with_pillow(
    image: Any,
    screen: tuple[int, int],
    region: tuple[int, int, int, int] | None = None,
    max_width: int = 0,
    quality: int = 0,
    grid: bool = False,
) -> Frame:
    """Downscale, optionally rule, and JPEG it.

    A 4K frame is about 8 MB raw and buys nothing: the model reads a button
    at 1280px just as well, and the smaller upload is the difference between
    a two-second look and a six-second one. A frame that is about to be acted
    on is worth more pixels than one that is only being described, which is
    what `max_width` is for.
    """
    from PIL import Image

    width, height = image.size
    limit = max(320, max_width or config.VISION_MAX_WIDTH)
    if width > limit:
        scaled = max(1, int(height * limit / width))
        image = image.resize((limit, scaled), Image.LANCZOS)

    frame_region = region
    screen_width = screen[0] or width
    screen_height = screen[1] or height

    # Taken before the ruler goes on, so two frames of the same screen match
    # even though the grid is drawn over both of them.
    signature = _fingerprint(image)
    if grid and config.VISION_GRID:
        bounds = (0.0, 0.0, 1.0, 1.0)
        if frame_region and screen_width and screen_height:
            bounds = (
                frame_region[0] / screen_width,
                frame_region[1] / screen_height,
                frame_region[2] / screen_width,
                frame_region[3] / screen_height,
            )
        try:
            image = _draw_ruler(image, bounds)
        except Exception as exc:  # a missing font must not cost the frame
            log.debug("Could not draw the coordinate ruler: %s", exc)

    import io

    buffer = io.BytesIO()
    image.convert("RGB").save(
        buffer,
        format="JPEG",
        quality=quality or config.VISION_JPEG_QUALITY,
        optimize=True,
    )
    return Frame(
        data=buffer.getvalue(),
        media_type="image/jpeg",
        width=image.size[0],
        height=image.size[1],
        screen_width=screen_width,
        screen_height=screen_height,
        region=frame_region,
        fingerprint=signature,
    )


def _clamp_region(
    region: tuple[float, float, float, float], screen: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """Turn a fractional rectangle into screen pixels, or None if unusable.

    Padded outwards a little, because a model asked to box the thing it wants
    to read boxes the text and not the frame around it, and a zoom cropped
    exactly to the words loses the button they sit on.
    """
    width, height = screen
    if width <= 0 or height <= 0:
        return None

    left = _coordinate(region[0], width)
    top = _coordinate(region[1], height)
    right = _coordinate(region[2], width)
    bottom = _coordinate(region[3], height)
    if None in (left, top, right, bottom):
        return None

    left, right = sorted((int(left), int(right)))
    top, bottom = sorted((int(top), int(bottom)))
    pad_x = max(8, int(width * 0.01))
    pad_y = max(8, int(height * 0.01))
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(width, right + pad_x)
    bottom = min(height, bottom + pad_y)

    if right - left < 16 or bottom - top < 16:
        return None
    return (left, top, right, bottom)


def capture_screen(
    region: tuple[int, int, int, int] | None = None,
    max_width: int = 0,
    quality: int = 0,
    grid: bool = False,
) -> Frame:
    """Grab the desktop, or one rectangle of it, smallest useful encoding.

    Three backends, tried in order of how little they cost:

    1. `mss` plus Pillow - a fast native grab, downscaled and JPEG-encoded.
    2. `mss` alone - PNG straight out of mss's own encoder. Bigger on the
       wire, but it means a machine without Pillow can still see.
    3. Pillow's `ImageGrab`, then `pyautogui` - both already present on most
       installs, both slower than mss.

    `region` crops to part of the screen *before* the downscale, which is the
    whole point of it: a dialog that is 400px wide on a 4K display arrives at
    the model as 400 real pixels rather than as the 130 that survive
    squeezing the desktop down to 1280. Small text is readable or it is not,
    and that is decided here.
    """
    screen = screen_size()
    errors: list[str] = []

    def _finish(image: Any, size: tuple[int, int]) -> Frame:
        cropped = image
        if region is not None:
            cropped = image.crop(region)
        return _encode_with_pillow(
            cropped, size, region=region, max_width=max_width, quality=quality, grid=grid
        )

    try:
        import mss

        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])
            if not screen[0]:
                screen = (shot.width, shot.height)
            try:
                from PIL import Image

                image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                return _finish(image, screen)
            except ImportError:
                # No Pillow: mss can still produce a PNG on its own. Larger,
                # but a bigger upload beats a blind assistant. No crop and no
                # ruler either - both of those are Pillow.
                import mss.tools

                data = mss.tools.to_png(shot.rgb, shot.size)
                return Frame(
                    data=data,
                    media_type="image/png",
                    width=shot.width,
                    height=shot.height,
                    screen_width=screen[0],
                    screen_height=screen[1],
                )
    except ImportError:
        errors.append("mss not installed")
    except Exception as exc:
        errors.append(f"mss failed: {exc}")

    for label, grab in (
        ("PIL.ImageGrab", _grab_with_imagegrab),
        ("pyautogui", _grab_with_pyautogui),
    ):
        try:
            image = grab()
        except ImportError:
            errors.append(f"{label} not installed")
            continue
        except Exception as exc:
            errors.append(f"{label} failed: {exc}")
            continue
        if image is not None:
            if not screen[0]:
                screen = image.size
            return _finish(image, screen)

    raise CaptureError("; ".join(errors) or "no screen capture backend available")


def _grab_with_imagegrab() -> Any:
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=True)


def _grab_with_pyautogui() -> Any:
    import pyautogui

    return pyautogui.screenshot()


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------
# One client for every vision call in the process, for the same reason the
# core loop shares one between the brain and the transcriber: a fresh client
# per screenshot throws away the TLS session and adds a full handshake to
# something the user is waiting on. Created on first use, so a session that
# never looks at the screen never opens a socket.
_client_lock = threading.Lock()
_client: httpx.Client | None = None


def _vision_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                timeout=config.VISION_TIMEOUT_S,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            )
        return _client


def close_vision_client() -> None:
    """Release the vision client. Safe to call when one was never opened."""
    global _client
    with _client_lock:
        if _client is not None and not _client.is_closed:
            _client.close()
        _client = None


class VisionError(RuntimeError):
    """The vision model could not be reached, or answered unusably.

    The message is phrased as plain English throughout, because it may end up
    at a speech synthesiser. A raw JSON error body read aloud is the worst
    possible answer.

    `no_such_model` marks the one recoverable case: this particular model is
    not usable on this account, so the next rung of the fallback ladder is
    worth trying. Everything else - a bad key, a rate limit, a timeout - is
    about the request rather than the model, and retrying it with a different
    name would only waste the user's time.
    """

    def __init__(self, message: str, no_such_model: bool = False) -> None:
        super().__init__(message)
        self.no_such_model = no_such_model


# Groq answers 404 for a model that never existed on this account and 400 for
# one that has been retired. To a caller looking for a model that works those
# are the same thing, and treating only the 404 that way is what made the
# ladder stop at its second rung.
_NO_SUCH_MODEL = ("model_not_found", "does not exist", "decommissioned", "no longer supported")


def _vision_provider() -> str:
    return (config.VISION_PROVIDER or config.LLM_PROVIDER or "groq").lower()


# How much of the per-minute token budget the provider says is left, or None
# when it does not say. Module-level rather than passed around because every
# vision request updates it and the only reader is the task loop, which is
# several frames away from the socket.
_budget_remaining: int | None = None


def vision_budget() -> int | None:
    """Tokens left in the vision model's window, or None if unknown.

    None is not zero and must not be treated as it: Gemini states no such
    header, and a provider that says nothing should be met by a loop that
    behaves exactly as it did before any of this existed.
    """
    return _budget_remaining


def _note_budget(headers: Any) -> None:
    """Record what the provider said is left in the window.

    Deliberately total: a header that is missing, empty or not a number
    leaves the previous reading alone rather than clearing it, because a
    single odd response should not talk the loop into thinking it has an
    unlimited budget or none at all.
    """
    global _budget_remaining
    try:
        raw = headers.get("x-ratelimit-remaining-tokens")
    except AttributeError:
        return
    if raw is None:
        return
    try:
        _budget_remaining = int(str(raw).strip())
    except (TypeError, ValueError):
        return


def ask_vision(frame: Frame, prompt: str, system: str = "") -> str:
    """Post one frame and one question; return the model's text.

    Provider-shaped by hand, like `ev.brain`, and for the same reason: both
    are a single JSON POST, and the vendor SDKs that would save ten lines
    here cost tens of megabytes of resident memory.
    """
    if not config.VISION_ENABLED:
        raise VisionError("Screen vision is switched off in the config.")

    provider = _vision_provider()
    if provider == "gemini":
        return _ask_gemini(frame, prompt, system)
    return _ask_groq(frame, prompt, system)


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    """One vision request, waiting out a rate limit once if it is short.

    The brain already does this, and vision needs it more rather than less.
    A screen task is a dozen requests in a row against the same per-minute
    budget, so it is the thing most likely to meet a 429 - and it meets one
    half way through, with several steps of real work already done. Failing
    there throws that away and leaves the desktop in a state nobody asked
    for. The server says how long its window is; waiting is E.V.'s job, not
    the user's.
    """
    from ev.brain import _retry_after

    response = None
    for attempt in range(2):
        try:
            response = _vision_client().post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise VisionError("Looking at the screen timed out.") from exc
        except httpx.HTTPError as exc:
            raise VisionError(f"Couldn't reach the vision model: {exc}") from exc

        _note_budget(response.headers)

        if response.status_code != 429 or attempt:
            break

        wait = _retry_after(response.headers)
        if wait is None or wait > config.LLM_RATE_LIMIT_MAX_WAIT_S:
            break
        log.info("Vision rate limited; waiting %.1fs before one retry", wait)
        time.sleep(wait)

    if response.status_code == 401:
        raise VisionError("The vision model rejected the API key.")
    if response.status_code == 429:
        # Only reached once the wait above has been tried, so this really is
        # "still busy" rather than "busy right now".
        raise VisionError("Rate limited on vision. Give it a few seconds.")
    if response.status_code >= 400:
        body = response.text.lower()
        if response.status_code == 404 or any(
            marker in body for marker in _NO_SUCH_MODEL
        ):
            raise VisionError(
                "That vision model isn't available on this account. "
                "Set EV_GROQ_VISION_MODEL to one that is.",
                no_such_model=True,
            )
        raise VisionError(f"The vision model returned {response.status_code}.")

    try:
        return response.json()
    except ValueError as exc:
        raise VisionError("The vision model sent back something unreadable.") from exc


def _ask_groq(frame: Frame, prompt: str, system: str) -> str:
    if not config.GROQ_API_KEY:
        raise VisionError("GROQ_API_KEY is not set, so I can't see the screen.")

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": frame.data_url}},
            ],
        }
    )

    # Groq's vision catalogue varies per account in exactly the way the chat
    # catalogue does, so the same fallback ladder applies rather than a 404
    # on every single look at the screen.
    models = [config.GROQ_VISION_MODEL, *config.GROQ_VISION_FALLBACKS]
    last: VisionError | None = None
    for model in models:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": config.VISION_TEMPERATURE,
            "max_tokens": config.VISION_MAX_TOKENS,
        }
        try:
            data = _post_json(
                f"{config.GROQ_BASE_URL}/chat/completions",
                payload,
                {"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            )
        except VisionError as exc:
            if not exc.no_such_model:
                raise
            log.info("Vision model %r unavailable; trying the next one", model)
            last = exc
            continue

        if model != config.GROQ_VISION_MODEL:
            log.warning("Falling back to vision model %r", model)
            config.GROQ_VISION_MODEL = model

        choices = data.get("choices") or []
        if not choices:
            raise VisionError("The vision model had nothing to say about that.")
        return str(choices[0].get("message", {}).get("content") or "").strip()

    raise last or VisionError("No usable vision model on this account.")


def _ask_gemini(frame: Frame, prompt: str, system: str) -> str:
    if not config.GEMINI_API_KEY:
        raise VisionError("GEMINI_API_KEY is not set, so I can't see the screen.")

    payload: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": frame.media_type,
                            "data": frame.base64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": config.VISION_TEMPERATURE,
            "maxOutputTokens": config.VISION_MAX_TOKENS,
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    url = (
        f"{config.GEMINI_BASE_URL}/models/"
        f"{config.GEMINI_VISION_MODEL}:generateContent"
    )
    data = _post_json(url, payload, {"x-goog-api-key": config.GEMINI_API_KEY})

    candidates = data.get("candidates") or []
    if not candidates:
        raise VisionError("The vision model had nothing to say about that.")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(str(part.get("text", "")) for part in parts).strip()


# ---------------------------------------------------------------------------
# The hands
# ---------------------------------------------------------------------------
_gui_lock = threading.Lock()
_gui_module: Any = None


def _gui() -> Any:
    """The configured `pyautogui`, or None if it cannot be used here.

    A function rather than a module-level import so that the cost is paid on
    first use, and so the tests can replace the whole backend with a recorder
    instead of driving the developer's actual mouse.
    """
    global _gui_module
    with _gui_lock:
        if _gui_module is not None:
            return _gui_module
        try:
            import pyautogui
        except Exception as exc:  # ImportError, or no display
            log.warning("pyautogui unavailable: %s", exc)
            return None
        # The corner failsafe aborts mid-drag if the pointer happens to pass
        # through 0,0, which turns a legitimate action into a half-finished
        # one. The confirmation gate is the safety mechanism here, not a
        # screen corner.
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0
        _gui_module = pyautogui
        return _gui_module


# ---------------------------------------------------------------------------
# Clipboard, for text that cannot be typed
# ---------------------------------------------------------------------------
# `pyautogui.write` presses one key per character against the current
# keyboard layout, which has two consequences worth designing around. It
# cannot produce a character the layout has no key for - an em dash, an
# accent, an emoji - and silently drops it. And at a keystroke every few
# milliseconds a paragraph takes long enough for an editor's autocomplete to
# interrupt it half way through and eat the rest.
#
# Pasting has neither problem. It is one Ctrl+V, it is exact, and it goes
# through the same focused window. The user's own clipboard is saved and put
# back, because silently replacing it is the kind of small rudeness that
# makes an assistant untrustworthy.
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _clipboard_api() -> Any:
    """user32 and kernel32 with the handle prototypes declared.

    Declaring them is not optional housekeeping. A Windows HANDLE is 64 bits
    on a 64-bit build and ctypes assumes `int` - 32 bits - for any function
    it has not been told about, so an undeclared `GetClipboardData` returns a
    truncated pointer and the `GlobalLock` on it takes the process down. This
    cost one segfault to learn.
    """
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    return user32, kernel32


def _clipboard_read() -> str | None:
    """The clipboard's text, or None if there is none or it cannot be read."""
    if not IS_WINDOWS:
        return None
    import ctypes

    try:
        user32, kernel32 = _clipboard_api()
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("Could not reach the clipboard API: %s", exc)
        return None

    if not user32.OpenClipboard(0):
        return None
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception as exc:
        log.debug("Could not read the clipboard: %s", exc)
        return None
    finally:
        user32.CloseClipboard()


def _clipboard_write(text: str) -> bool:
    """Put text on the clipboard. False if Windows would not take it."""
    if not IS_WINDOWS:
        return False
    import ctypes

    try:
        user32, kernel32 = _clipboard_api()
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("Could not reach the clipboard API: %s", exc)
        return False

    size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
    if not user32.OpenClipboard(0):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, ctypes.create_unicode_buffer(text), size)
        finally:
            kernel32.GlobalUnlock(handle)
        # After this succeeds the system owns the block, so it must not be
        # freed here. On failure it leaks one small allocation, which is the
        # better of the two mistakes available.
        return bool(user32.SetClipboardData(_CF_UNICODETEXT, handle))
    except Exception as exc:
        log.debug("Could not write the clipboard: %s", exc)
        return False
    finally:
        user32.CloseClipboard()


def _needs_paste(text: str) -> bool:
    """Whether this string should be pasted rather than typed."""
    if len(text) > max(1, config.COMPUTER_PASTE_THRESHOLD):
        return True
    return any(ord(char) > 127 for char in text)


def _enter_text(gui: Any, text: str) -> str:
    """Get `text` into the focused window. Returns how it was done.

    Typing stays the default for short plain strings: it is what an
    application expects, it triggers the key handlers a paste does not, and
    some fields refuse a paste outright. The clipboard is for the cases
    typing genuinely cannot serve.
    """
    if _needs_paste(text) and IS_WINDOWS:
        previous = _clipboard_read()
        if _clipboard_write(text):
            try:
                gui.hotkey("ctrl", "v")
                time.sleep(0.12)
                return "pasted"
            finally:
                # Restoring is best-effort and deliberately not fatal: the
                # text is already in the window by this point, and failing
                # the action over a clipboard that would not restore would
                # throw away work that succeeded.
                if previous is not None:
                    _clipboard_write(previous)
        log.info("Clipboard unavailable; falling back to typing %d chars", len(text))

    gui.write(text, interval=config.COMPUTER_TYPE_INTERVAL_S)
    return "typed"


_PERCENT = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*%\s*$")


def _coordinate(raw: Any, extent: int) -> int | None:
    """Turn one model-supplied coordinate into a pixel on this screen.

    Values from 0 to 1 are fractions of the screen, which is what the schema
    asks for and what survives the downscaling the model saw. Anything
    larger is taken as a pixel already, because a model that has been told
    the screen is 1920 wide will sometimes answer in pixels anyway, and
    refusing that is pedantry. A trailing percent sign is also accepted.

    Returns None when there is no number in there at all, so the caller can
    say so rather than clicking the top-left corner.
    """
    if raw is None or extent <= 0:
        return None
    if isinstance(raw, bool):
        return None

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        percent = _PERCENT.match(text)
        if percent:
            value = float(percent.group(1)) / 100.0
        else:
            try:
                value = float(text)
            except ValueError:
                return None
    elif isinstance(raw, (int, float)):
        value = float(raw)
    else:
        return None

    if -1.0 <= value <= 1.0:
        pixels = round(value * extent)
    elif abs(value) < 2.0 and not float(value).is_integer():
        # 1.02 is a fraction that overshot the right edge. Reading it as
        # "pixel number one" would put the click in the corner of the screen,
        # which is both wrong and the one place it could do real damage.
        pixels = round(value * extent)
    else:
        pixels = round(value)

    # Clamping rather than refusing: the edge of the screen is a perfectly
    # good place to click, and a model that overshoots by a percent still
    # meant the edge.
    return max(0, min(extent - 1, int(pixels)))


def _resolve_point(x: Any, y: Any) -> tuple[int, int] | None:
    width, height = screen_size()
    px = _coordinate(x, width)
    py = _coordinate(y, height)
    if px is None or py is None:
        return None
    return px, py


def _number(raw: Any, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _gate(description: str, confirmed: bool, speech: str, **data: Any) -> ToolResult | None:
    """Hold a risky action for a spoken yes, or let it through.

    Returns a `ToolResult.confirm` to be handed straight back, or None when
    the action may proceed. `data` is echoed into the confirmation so the
    core loop can replay the identical call after the user says yes - the
    keys must therefore match the tool's own schema properties.
    """
    if confirmed or not config.COMPUTER_CONFIRM_RISKY:
        return None
    verdict = classify_gui(description)
    if not verdict.needs_confirmation:
        return None
    return ToolResult.confirm(
        speech,
        f"Awaiting confirmation: {description} ({verdict.reason}).",
        # Carried so the core loop knows how firm a yes this one needs. It
        # is popped before the call is replayed, like every other key here
        # that is not a schema property.
        reason=verdict.reason,
        **data,
    )


# ---------------------------------------------------------------------------
# take_screenshot
# ---------------------------------------------------------------------------
_LOOK_SYSTEM = (
    "You are the eyes of a voice assistant looking at a Windows desktop. "
    "Answer in one or two short spoken sentences, under thirty words. "
    "Plain speech: no markdown, no bullet points, no headings, no labels, "
    "no coordinates unless you were asked for them. Describe what is "
    "actually on the screen, never what you expect to be there."
)


def take_screenshot(
    question: str = "",
    save_as: str = "",
    region: str = "",
    **_: object,
) -> ToolResult:
    """Look at the screen and answer a question about it.

    `region` is four fractions - left, top, right, bottom - and it is how
    small text gets read. Squeezing a 4K desktop into 1280px throws away the
    pixels that words are made of, so "what does that error say" against a
    whole-screen frame is a guess; against a crop of the dialog it is
    reading. Omitted, it looks at everything.

    `save_as` is the one path that writes a frame to disk, and it goes
    through `file_manager.resolve_user_path`, so the same root check that
    governs every other file E.V. writes governs this one too. A refused
    path is reported, never quietly retargeted.
    """
    if not config.COMPUTER_USE_ENABLED:
        return ToolResult.failure(
            "Screen access is switched off.",
            "EV_COMPUTER_USE_ENABLED is false; no screenshot was taken.",
        )

    crop = _parse_region(region)
    try:
        # No ruler on a plain look: the grid is there to make a click land,
        # and a question about the screen is not about to click anything.
        # The saved copy is what the user asked for, not a marked-up one.
        frame = capture_screen(
            region=crop,
            max_width=config.VISION_ZOOM_MAX_WIDTH if crop else 0,
        )
    except CaptureError as exc:
        return ToolResult.failure(
            "I can't see the screen from here.",
            f"Screen capture failed: {exc}. Install mss or Pillow to fix it.",
        )

    saved = ""
    if save_as.strip():
        try:
            saved = _save_frame(frame, save_as)
        except Exception as exc:
            return ToolResult.failure(
                "Couldn't save that where you asked.",
                f"Screenshot save refused: {exc}",
            )

    if not config.VISION_ENABLED:
        speech = "Got the screen." + (" Saved it too." if saved else "")
        return ToolResult.success(
            speech,
            f"Captured {frame.screen_width}x{frame.screen_height}. Vision is "
            f"disabled, so the frame was not described."
            + (f" Saved to {saved}." if saved else ""),
        )

    asked = question.strip() or "What is on this screen right now?"
    try:
        answer = ask_vision(frame, asked, _LOOK_SYSTEM)
    except VisionError as exc:
        return ToolResult.failure(str(exc), f"Vision call failed: {exc}")

    if not answer:
        return ToolResult.failure(
            "I looked, but I couldn't make sense of it.",
            "The vision model returned an empty description.",
        )

    seen = "part of the screen" if crop else "the screen"
    detail = (
        f"Screen is {frame.screen_width}x{frame.screen_height}. "
        f"Looking at {seen}: {answer}"
    )
    open_windows = _screen_context()
    if open_windows:
        # The window list travels with the answer so the next turn knows what
        # is actually open, rather than re-deriving it from a description.
        detail += " Windows open, front to back: " + open_windows
    if saved:
        detail += f" Saved a copy to {saved}."
    return ToolResult.success(answer, detail, screenshot=saved)


def _parse_region(raw: str) -> tuple[int, int, int, int] | None:
    """Read "0.1,0.2,0.5,0.6" into a pixel rectangle, or None.

    Anything unparseable returns None, which means "look at the whole
    screen". A malformed crop should cost detail, never the look itself.
    """
    text = (raw or "").strip()
    if not text:
        return None
    parts = [chunk for chunk in re.split(r"[,\s]+", text) if chunk]
    if len(parts) != 4:
        log.debug("Ignoring unusable screenshot region %r", raw)
        return None
    return _clamp_region((parts[0], parts[1], parts[2], parts[3]), screen_size())


def _save_frame(frame: Frame, destination: str) -> str:
    """Write a frame to a user directory, root-checked like any other file.

    Imported here rather than at module scope so that `computer_use` does not
    drag the whole file-manager surface into a session that never saves one.
    """
    from tools.file_manager import friendly, resolve_user_path

    suffix = ".jpg" if frame.media_type == "image/jpeg" else ".png"
    name = destination.strip()
    if not name.lower().endswith((".jpg", ".jpeg", ".png")):
        name = f"{name}{suffix}"

    path = resolve_user_path(name, default=config.FILE_DEFAULT_DIR)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame.data)
    return friendly(path)


# ---------------------------------------------------------------------------
# mouse_action
# ---------------------------------------------------------------------------
_MOUSE_ALIASES = {
    "left_click": "click",
    "single_click": "click",
    "leftclick": "click",
    "doubleclick": "double_click",
    "dbl_click": "double_click",
    "rightclick": "right_click",
    "context_click": "right_click",
    "middleclick": "middle_click",
    "move_to": "move",
    "hover": "move",
    "drag_to": "drag",
}


def mouse_action(
    action: str = "click",
    x: str = "",
    y: str = "",
    to_x: str = "",
    to_y: str = "",
    amount: str = "",
    label: str = "",
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    """Move, click, drag or scroll the real pointer.

    `label` is what the model believes it is clicking, and it is not
    decoration: it is the string the confirmation gate reads. An action with
    no label is an action nobody can review, so the model is told to always
    supply one.
    """
    if not config.COMPUTER_USE_ENABLED:
        return ToolResult.failure(
            "Screen control is switched off.",
            "EV_COMPUTER_USE_ENABLED is false; no mouse action was taken.",
        )

    verb = (action or "click").strip().lower().replace("-", "_").replace(" ", "_")
    verb = _MOUSE_ALIASES.get(verb, verb)

    described = f"{verb.replace('_', ' ')} {label}".strip()
    held = _gate(
        described,
        confirmed,
        f"That clicks {label or 'that'}. Confirm?" if label else "That's a live click. Confirm?",
        action=verb,
        x=str(x),
        y=str(y),
        to_x=str(to_x),
        to_y=str(to_y),
        amount=str(amount),
        label=label,
    )
    if held is not None:
        return held

    gui = _gui()
    if gui is None:
        return ToolResult.failure(
            "I can't reach the mouse from here.",
            "pyautogui is unavailable, so no pointer action ran.",
        )

    if verb == "scroll":
        clicks = int(_number(amount, -400))
        point = _resolve_point(x, y)
        if point is not None:
            gui.moveTo(point[0], point[1], duration=config.COMPUTER_MOVE_DURATION_S)
        gui.scroll(clicks)
        way = "down" if clicks < 0 else "up"
        return ToolResult.success(f"Scrolled {way}.", f"Scrolled {clicks} clicks {way}.")

    point = _resolve_point(x, y)
    if point is None:
        return ToolResult.failure(
            "I need a spot on the screen for that.",
            f"mouse_action '{verb}' needs x and y as fractions of the screen "
            "between 0 and 1. Take a screenshot first if you don't know where.",
        )
    px, py = point
    where = label or f"{px},{py}"

    if verb == "move":
        gui.moveTo(px, py, duration=config.COMPUTER_MOVE_DURATION_S)
        return ToolResult.success("Pointer's there.", f"Moved the pointer to {px},{py}.")

    if verb == "drag":
        end = _resolve_point(to_x, to_y)
        if end is None:
            return ToolResult.failure(
                "You didn't say where to drag it to.",
                "mouse_action 'drag' needs to_x and to_y as well as x and y.",
            )
        gui.moveTo(px, py, duration=config.COMPUTER_MOVE_DURATION_S)
        gui.dragTo(end[0], end[1], duration=max(0.2, config.COMPUTER_MOVE_DURATION_S * 3), button="left")
        return ToolResult.success(
            "Dragged it.", f"Dragged from {px},{py} to {end[0]},{end[1]}."
        )

    buttons = {"click": "left", "double_click": "left", "right_click": "right", "middle_click": "middle"}
    if verb not in buttons:
        return ToolResult.failure(
            "I don't know that mouse action.",
            f"Unknown mouse action '{action}'. Valid: move, click, "
            "double_click, right_click, middle_click, drag, scroll.",
        )

    gui.moveTo(px, py, duration=config.COMPUTER_MOVE_DURATION_S)
    gui.click(x=px, y=py, clicks=2 if verb == "double_click" else 1,
              interval=0.08, button=buttons[verb])

    spoken = {
        "click": "Clicked.",
        "double_click": "Double-clicked.",
        "right_click": "Right-clicked.",
        "middle_click": "Middle-clicked.",
    }[verb]
    return ToolResult.success(spoken, f"{verb} at {px},{py} on {where}.")


# ---------------------------------------------------------------------------
# keyboard_action
# ---------------------------------------------------------------------------
_KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "control": "ctrl",
    "windows": "win",
    "cmd": "win",
    "option": "alt",
    "pgdn": "pagedown",
    "pgup": "pageup",
    "del": "delete",
}


def _split_keys(raw: str) -> list[str]:
    """Turn 'ctrl+shift+t' or 'ctrl shift t' into pyautogui key names."""
    parts = [p for p in re.split(r"[+\s,]+", (raw or "").strip().lower()) if p]
    return [_KEY_ALIASES.get(part, part) for part in parts]


def keyboard_action(
    action: str = "type",
    text: str = "",
    keys: str = "",
    label: str = "",
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    """Type text, or send a hotkey, to whatever currently has focus.

    Typed text is classified twice. `classify_gui` reads it as an intent -
    "send the resignation email" wants a yes. `classify` reads it as a shell
    command, because text typed into a focused terminal *is* a shell command,
    and a blocked pattern must not become runnable just because it arrived
    through the keyboard rather than through `terminal_command`.
    """
    if not config.COMPUTER_USE_ENABLED:
        return ToolResult.failure(
            "Keyboard control is switched off.",
            "EV_COMPUTER_USE_ENABLED is false; nothing was typed.",
        )

    verb = (action or "type").strip().lower().replace("-", "_").replace(" ", "_")
    if verb in {"type_text", "write", "text"}:
        verb = "type"
    if verb in {"hotkey", "shortcut", "combo"}:
        verb = "press"
    if verb in {"key", "key_press", "keypress"}:
        verb = "press"

    payload = text if verb == "type" else (keys or text)
    if not str(payload).strip():
        return ToolResult.failure(
            "You didn't say what to type.",
            f"keyboard_action '{verb}' needs {'text' if verb == 'type' else 'keys'}.",
        )

    if verb == "type":
        # A shell command typed into a focused terminal is a shell command.
        # The blocked list exists for exactly these strings and must not be
        # bypassed by the route they arrive on.
        shell_verdict = classify(text)
        if shell_verdict.is_blocked:
            return ToolResult.failure(
                "Not typing that one.",
                f"Refused to type a blocked command ({shell_verdict.reason}). "
                "Do not retry; ask the user to run it themselves.",
            )

    described = f"{label} {payload}".strip()
    held = _gate(
        described,
        confirmed,
        f"That types {label or 'that'} for real. Confirm?",
        action=verb,
        text=text,
        keys=keys,
        label=label,
    )
    if held is not None:
        return held

    gui = _gui()
    if gui is None:
        return ToolResult.failure(
            "I can't reach the keyboard from here.",
            "pyautogui is unavailable, so nothing was typed.",
        )

    if verb == "type":
        how = _enter_text(gui, text)
        shown = text if len(text) <= 60 else text[:57] + "..."
        where = window.foreground_title() or "the focused window"
        # Naming the window that received the text is what lets the model
        # notice it went somewhere unintended. "Typed." into the wrong
        # application looks exactly like success from here.
        return ToolResult.success("Typed.", f"{how.capitalize()} into {where}: {shown}")

    if verb == "press":
        combo = _split_keys(str(payload))
        if not combo:
            return ToolResult.failure(
                "That wasn't a key I recognised.", f"Could not parse keys {payload!r}."
            )
        if len(combo) == 1:
            gui.press(combo[0])
        else:
            gui.hotkey(*combo)
        return ToolResult.success("Sent.", f"Pressed {'+'.join(combo)}.")

    return ToolResult.failure(
        "I don't know that keyboard action.",
        f"Unknown keyboard action '{action}'. Valid: type, press.",
    )


# ---------------------------------------------------------------------------
# screen_task - the vision-driven loop
# ---------------------------------------------------------------------------
# Capture -> parse state -> pick a target -> act -> look again. The loop is
# the whole point: a single screenshot tells the model where a button is
# *now*, and by the time the click lands the screen has already moved on.
#
# Two ceilings, both load-bearing. `SCREEN_TASK_MAX_STEPS` stops a run that
# keeps clicking on a page which never changes, and `SCREEN_TASK_TIMEOUT_S`
# stops one where every step is slow rather than repeated. Without them an
# autonomous mouse is a mouse nobody can get back.
_STEP_SYSTEM = """You are driving a Windows desktop for a voice assistant, one look at a time.

You are shown the current screen with a coordinate grid ruled over it, and a
list of the windows that are actually open. Reply with ONE JSON object and
nothing else:

{"observation": "the one thing on screen that decides the next move",
 "plan": "the remaining steps in one line - first reply only",
 "actions": [ ... one or more actions ... ]}

An action is one of these:
{"action":"launch","app":"notepad","arguments":"optional file or folder to open with it"}
{"action":"focus","window":"part of the window title"}
{"action":"wait","window":"part of a title to wait for"}   or   {"action":"wait","amount":"2"}
{"action":"click","x":0.42,"y":0.13,"label":"the File menu"}
{"action":"double_click" or "right_click" or "middle_click" or "move", same fields as click}
{"action":"drag","x":0.2,"y":0.3,"to_x":0.6,"to_y":0.3,"label":"the slider"}
{"action":"scroll","amount":-400,"label":"the message list"}
{"action":"type","text":"the exact characters","label":"what this is for"}
{"action":"press","keys":"ctrl+s","label":"what this is for"}
{"action":"zoom","x":0.3,"y":0.4,"to_x":0.7,"to_y":0.6,"label":"read this closely"}
{"action":"done","speech":"one short spoken sentence"}
{"action":"fail","speech":"why this cannot be done from here"}

Rules:
- The red border and the status panel belong to E.V.'s own overlay. Ignore
  them; they are not part of any application, and nothing is ever clicked there.
- Coordinates are FRACTIONS of the whole screen, 0 to 1. Read them off the grid:
  the yellow labels along the top are x, the ones down the left are y. On a
  zoomed image those labels still mean fractions of the whole screen, so use
  them exactly as they read.
- Prefer "launch" over hunting for an icon, and "focus" over clicking the
  taskbar. They name the thing instead of aiming at it, so they cannot miss.
- Prefer a keyboard shortcut over a click when the application has one. Ctrl+P
  in VS Code beats finding a file in the sidebar by eye.
- After launching anything, "wait" for its window before acting on it.
- Put several actions in one reply only when none of them needs a fresh look
  first - typing a line and then pressing Enter. Anything you must see before
  you can aim at it goes in the next reply.
- Never put "type" in the same reply as "launch" or "focus". An application
  that was already open comes back with a document in it, and text typed into
  a window you have not looked at goes into the middle of the user's work.
  Launch, wait, then look, and only then decide where the text goes.
- If the goal is to write something and the application came up showing work
  that is already there, start a new document first - Ctrl+N in an editor, a
  new tab - rather than adding to it. The user asked for their text somewhere,
  not for it to be appended to whatever they last had open. Only type into
  existing content when the goal actually says to.
- Always fill in "label". It is the words the user is asked to approve.
- Never invent a coordinate for something you cannot see on this screen. Zoom
  in, or use "fail".
- Use "done" the moment the screen itself shows the goal is achieved, not when
  you have run out of ideas.
"""

_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)

# Actions the loop resolves itself rather than handing to a single-action
# tool. `zoom` changes what the next frame looks at, and `done`/`fail` end
# the run, so none of them is a thing that can be "performed".
_LOOP_ACTIONS = frozenset({"zoom", "done", "fail"})

# Actions after which the model does not know what it is looking at any
# more, so the batch has to stop and take a fresh frame.
#
# This is not a theoretical concern. "Open Notepad and type hello" was
# planned, correctly, as launch then wait then type - and on a machine where
# Notepad was already open on a page of the user's own notes, the "hello"
# landed in the middle of them. Launching something tells you it is running.
# It tells you nothing about what is in it, and typing into an application
# whose contents you have not looked at is writing into the dark.
#
# A trailing `wait` is allowed to ride along, because waiting is how a
# launch finishes rather than a new thing being done.
_BOUNDARY_ACTIONS = frozenset({"launch", "focus"})


def _parse_step(raw: str) -> dict[str, Any]:
    """Pull the step object out of the model's reply.

    Vision models wrap JSON in prose and code fences more often than not, so
    a plain `json.loads` on the whole reply fails on perfectly good output.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOB.search(text)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _step_actions(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """The actions in a reply, in order, however the model chose to shape it.

    Both forms are accepted because both keep turning up: a bare action
    object is what a model emits when there is only one thing to do, and the
    `actions` list is what it emits when asked for several. Rejecting either
    would mean losing a good step to a formatting preference.

    The batch is capped rather than refused. A model that returns nine
    actions has usually planned the whole task in one go, and the first two
    or three of those are still right - it is the ones that land after the
    screen has changed that are guesses.
    """
    raw = parsed.get("actions")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raw = [parsed] if parsed.get("action") else []

    actions = [item for item in raw if isinstance(item, dict) and item.get("action")]
    if not actions:
        return []

    trimmed = actions[: max(1, config.SCREEN_TASK_MAX_BATCH)]

    # A batch stops at the first action that ends the run, or that leaves the
    # screen in a state nobody has looked at yet.
    for position, action in enumerate(trimmed):
        verb = str(action.get("action", "")).strip().lower()
        if verb in _LOOP_ACTIONS:
            return trimmed[: position + 1]
        if verb in _BOUNDARY_ACTIONS:
            cut = position + 1
            while cut < len(trimmed) and str(
                trimmed[cut].get("action", "")
            ).strip().lower() == "wait":
                cut += 1
            return trimmed[:cut]
    return trimmed


def _apply_step(step: dict[str, Any], cancel: CancelToken | None = None) -> ToolResult:
    """Run one parsed action through the ordinary single-action tools.

    Going back through `mouse_action` and `keyboard_action` rather than
    touching pyautogui directly means the loop cannot skip the blocked-command
    check, and there is exactly one place where a click is performed. The same
    argument applies to `launch`: it goes through `open_app`, so a path-shaped
    argument still meets `FILE_ROOTS` on the way past.
    """
    verb = str(step.get("action", "")).strip().lower()
    label = str(step.get("label", "") or "")

    if verb == "launch":
        from tools.app_launcher import open_app

        app = str(step.get("app", "") or step.get("text", "") or label).strip()
        if not app:
            return ToolResult.failure(
                "I didn't catch which app.", "A launch step arrived with no 'app'."
            )
        return open_app(app=app, arguments=str(step.get("arguments", "") or ""))

    if verb == "focus":
        title = str(step.get("window", "") or step.get("text", "") or label).strip()
        if not title:
            return ToolResult.failure(
                "I didn't catch which window.", "A focus step arrived with no 'window'."
            )
        if window.focus_by_title(title, timeout=config.SCREEN_TASK_WAIT_S):
            return ToolResult.success("Focused.", f"Brought '{title}' to the front.")
        return ToolResult.failure(
            "That window wouldn't come forward.",
            f"No window matching '{title}' took focus within "
            f"{config.SCREEN_TASK_WAIT_S:.0f}s. It may not be open yet.",
        )

    if verb == "wait":
        title = str(step.get("window", "") or "").strip()
        if title:
            # Polling for the window beats sleeping for a guess in both
            # directions: an app that is already up costs nothing, and one
            # that is slow is actually waited for.
            found = window.wait_for_window(title, timeout=config.SCREEN_TASK_WAIT_S)
            if found is not None:
                return ToolResult.success("There it is.", f"Window '{title}' appeared.")
            return ToolResult.failure(
                "It never showed up.",
                f"Window '{title}' did not appear within "
                f"{config.SCREEN_TASK_WAIT_S:.0f}s.",
            )
        seconds = max(
            0.0, min(_number(step.get("amount", ""), 1.0), config.SCREEN_TASK_WAIT_S)
        )
        if cancel is not None:
            cancel.wait(seconds)
        else:
            time.sleep(seconds)
        return ToolResult.success("Waited.", f"Waited {seconds:g}s.")

    if verb in {"type", "press", "hotkey", "key"}:
        return keyboard_action(
            action="type" if verb == "type" else "press",
            text=str(step.get("text", "") or ""),
            keys=str(step.get("keys", "") or ""),
            label=label,
            # Already gated at the loop level; gating again here would ask
            # the same question twice for the same click.
            confirmed=True,
        )

    return mouse_action(
        action=verb,
        x=step.get("x", ""),
        y=step.get("y", ""),
        to_x=step.get("to_x", ""),
        to_y=step.get("to_y", ""),
        amount=step.get("amount", ""),
        label=label,
        confirmed=True,
    )


def _step_risk(step: dict[str, Any]) -> str:
    """Everything about a step that could make it risky, as one string."""
    return " ".join(
        str(step.get(key, "") or "")
        for key in ("action", "label", "text", "keys", "app", "arguments")
    ).strip()


def _describe_step(step: dict[str, Any]) -> str:
    """A short note for the history, e.g. "click the Save button"."""
    verb = str(step.get("action", "") or "?").strip().lower()
    subject = (
        str(step.get("label", "") or "")
        or str(step.get("app", "") or "")
        or str(step.get("window", "") or "")
        or str(step.get("keys", "") or "")
        or str(step.get("text", "") or "")[:30]
    )
    return f"{verb} {subject}".strip()


def _screen_context() -> str:
    """The window list as prompt text, or "" when it cannot be had.

    This is ground truth the frame cannot supply. A screenshot shows an
    editor; the window manager knows it is VS Code, that it is focused, and
    exactly which rectangle it owns. Given both, the model stops inferring
    the one thing it is worst at inferring.
    """
    try:
        width, height = screen_size()
        return window.describe_windows(width, height)
    except Exception as exc:  # pragma: no cover - inventory is best-effort
        log.debug("Could not list windows: %s", exc)
        return ""


def screen_task(
    task: str = "",
    max_steps: str = "",
    confirmed: bool = False,
    cancel: CancelToken | None = None,
    **_: object,
) -> ToolResult:
    """Drive the screen towards a goal, looking between every step.

    Confirmation works at the level of the whole run rather than per click.
    A risky step stops the loop and asks; saying yes re-runs `screen_task`
    with the same goal, which is safe precisely because the loop is
    stateless - it reads the screen fresh every time, so it resumes from
    wherever the screen actually got to rather than replaying what it already
    did.
    """
    goal = (task or "").strip()
    if not goal:
        return ToolResult.failure(
            "You didn't say what to do on screen.",
            "screen_task needs a task describing the goal.",
        )

    if not config.COMPUTER_USE_ENABLED:
        return ToolResult.failure(
            "Screen control is switched off.",
            "EV_COMPUTER_USE_ENABLED is false; the screen task did not run.",
        )
    if not config.VISION_ENABLED:
        return ToolResult.failure(
            "I can't see the screen, so I can't drive it.",
            "EV_VISION_ENABLED is false; screen_task needs vision.",
        )

    # The goal itself is gated up front. "Buy the first result" should ask
    # before the first click, not three clicks in at the checkout.
    held = _gate(
        goal,
        confirmed,
        f"That'll {goal.rstrip('.')}, for real. Want me to?",
        task=goal,
        max_steps=str(max_steps),
    )
    if held is not None:
        return held

    # The frame and the kill switch go up before the first look, for the
    # whole run: a pointer moving on its own with nothing on screen to say
    # why is indistinguishable from a machine somebody else has taken over.
    token = cancel if cancel is not None else CancelToken()
    with taking_over(goal, token) as hud:
        return _drive_screen(goal, max_steps, confirmed, token, hud)


def _drive_screen(
    goal: str,
    max_steps: str,
    confirmed: bool,
    cancel: CancelToken,
    hud: Any,
) -> ToolResult:
    """The look-act loop behind `screen_task`, run under the overlay."""
    ceiling = int(_number(max_steps, config.SCREEN_TASK_MAX_STEPS))
    ceiling = max(
        1, min(ceiling or config.SCREEN_TASK_MAX_STEPS, config.SCREEN_TASK_MAX_STEPS)
    )
    deadline = time.monotonic() + config.SCREEN_TASK_TIMEOUT_S

    history: list[str] = []
    plan = ""
    zoom: tuple[int, int, int, int] | None = None
    previous = ""
    stalled = 0

    # The ceiling counts actions, not looks. One reply may carry a small
    # batch, so bounding the loop alone would quietly allow three times the
    # steps the caller asked for - and `max_steps` is a promise about what
    # will happen to the screen, not about how often E.V. glances at it.
    for index in range(1, ceiling + 1):
        if len(history) >= ceiling:
            break
        if was_cancelled(cancel):
            return _stopped(goal, history, f"cancelled before step {index}")
        if time.monotonic() > deadline:
            return ToolResult.success(
                "Ran out of time on that one.",
                f"screen_task '{goal}' hit the {config.SCREEN_TASK_TIMEOUT_S:.0f}s "
                f"ceiling after {len(history)} step(s): {'; '.join(history) or 'none'}.",
            )

        # Stop one step short of the wall rather than walking into it. A
        # vision step costs about 1900 against the per-minute budget and the
        # charge does not shrink with the frame, so there is no cheaper
        # version of this step to fall back on - only the choice between
        # stopping with something to report and being cut off mid-task with
        # the desktop in a state nobody has described.
        #
        # With no steps behind it there is nothing to hand back and nothing
        # to lose, so the first look is always attempted and a real 429 is
        # left to the wait in `_post_json`.
        budget = vision_budget()
        if (
            history
            and budget is not None
            and config.VISION_BUDGET_FLOOR
            and budget < config.VISION_BUDGET_FLOOR
        ):
            log.info(
                "Stopping screen_task early: %d vision tokens left in the window",
                budget,
            )
            return ToolResult.success(
                "I'm out of budget for looking at the screen - "
                "give it a minute and I'll pick this up.",
                f"screen_task '{goal}' stopped after {len(history)} step(s) with "
                f"{budget} vision tokens left in the per-minute window: "
                f"{'; '.join(history)}. Not finished - the rest still needs doing.",
            )

        hud.note(f"Step {len(history) + 1} of {ceiling}: looking at the screen.")
        try:
            frame = capture_screen(
                region=zoom,
                max_width=config.VISION_TASK_MAX_WIDTH,
                quality=config.VISION_TASK_JPEG_QUALITY,
                grid=True,
            )
        except CaptureError as exc:
            return ToolResult.failure(
                "I lost sight of the screen.", f"Capture failed mid-task: {exc}"
            )
        zoomed = zoom is not None
        zoom = None  # a zoom is one look, not a mode to get stuck in

        # Whether the last action did anything at all. A model cannot tell
        # from a single frame that it is clicking a dead button, because a
        # dead button looks exactly like the one it just clicked - so it
        # clicks again until the step budget runs out. Comparing consecutive
        # frames is the only place that fact exists.
        nudge = ""
        if not zoomed:
            if previous and frame.fingerprint:
                if frames_match(previous, frame.fingerprint):
                    stalled += 1
                    nudge = (
                        "\nThe screen has NOT changed since your last action, so it "
                        "did nothing visible. Try another route; do not repeat it."
                    )
                else:
                    stalled = 0
            previous = frame.fingerprint or previous

        if stalled >= 2:
            return ToolResult.failure(
                "That's not going anywhere.",
                f"screen_task '{goal}' stalled: the screen did not change after "
                f"{stalled} consecutive actions. Done: "
                f"{'; '.join(history) or 'nothing'}. The target may need a "
                "different application, or a real click from the user.",
            )

        windows = _screen_context()
        prompt = (
            f"Goal: {goal}\n"
            + (f"Your plan: {plan}\n" if plan else "")
            + f"Step {index} of at most {ceiling}.\n"
            + (
                "This image is a ZOOM into part of the screen. The grid labels "
                "are still whole-screen fractions.\n"
                if zoomed
                else ""
            )
            + (f"Windows open, front to back:\n{windows}\n" if windows else "")
            + f"Done so far: {'; '.join(history[-6:]) if history else 'nothing yet'}."
            + nudge
            + "\nWhat next?"
        )
        try:
            reply = _parse_step(ask_vision(frame, prompt, _STEP_SYSTEM))
        except VisionError as exc:
            return ToolResult.failure(str(exc), f"Vision failed mid-task: {exc}")

        actions = _step_actions(reply)
        if not actions:
            return ToolResult.failure(
                "I couldn't work out the next move.",
                f"The vision model returned no usable step at step {index}. "
                f"Done so far: {'; '.join(history) or 'nothing'}.",
            )

        if not plan:
            plan = str(reply.get("plan", "") or "").strip()[:200]

        # Every action in the batch is inspected before any of them runs, for
        # the same reason `browser_task` inspects its whole script: a batch is
        # known in full up front, so a risky third action should be asked
        # about before the first one happens.
        if not confirmed and config.COMPUTER_CONFIRM_RISKY:
            for action in actions:
                verdict = classify_gui(_step_risk(action))
                if verdict.needs_confirmation:
                    label = str(action.get("label", "") or "that")
                    return ToolResult.confirm(
                        f"Next step {verdict.reason}: {label}. Go ahead?",
                        f"Held at step {index} of '{goal}': {_describe_step(action)} "
                        f"({verdict.reason}). Confirming re-reads the screen and "
                        "carries on from there.",
                        task=goal,
                        max_steps=str(ceiling),
                    )

        for action in actions:
            if was_cancelled(cancel):
                return _stopped(goal, history, f"cancelled during step {index}")

            verb = str(action.get("action", "")).strip().lower()
            spoken = str(action.get("speech", "") or "").strip()

            if verb == "done":
                return ToolResult.success(
                    spoken or "That's done.",
                    f"screen_task '{goal}' finished in {len(history)} step(s): "
                    f"{'; '.join(history) or 'no actions needed'}.",
                )
            if verb == "fail":
                return ToolResult.failure(
                    spoken or "I couldn't get that done.",
                    f"screen_task '{goal}' gave up at step {index}: "
                    f"{spoken or 'no reason given'}. Done so far: "
                    f"{'; '.join(history) or 'nothing'}.",
                )
            if verb == "zoom":
                region = _clamp_region(
                    (
                        action.get("x", 0),
                        action.get("y", 0),
                        action.get("to_x", 1),
                        action.get("to_y", 1),
                    ),
                    screen_size(),
                )
                if region is None:
                    history.append("zoom (unusable region)")
                    continue
                zoom = region
                history.append(
                    f"looked closely at {action.get('label') or 'the screen'}"
                )
                break

            # Checked inside the batch as well as around it: a reply
            # carrying three actions must not step over the ceiling by two
            # just because it arrived all at once.
            if len(history) >= ceiling:
                break

            described = _describe_step(action)
            hud.note(described)
            result = _apply_step(action, cancel)
            history.append(described if result.ok else f"{described} (failed)")
            if not result.ok:
                return ToolResult.failure(
                    f"Got stuck: {result.speech}",
                    f"screen_task '{goal}' failed at step {index}: {result.detail}",
                )

            # Let the screen catch up before the next action, or the next
            # frame shows the state this one just changed.
            if cancel is not None:
                cancel.wait(config.COMPUTER_ACTION_PAUSE_S)
            else:
                time.sleep(config.COMPUTER_ACTION_PAUSE_S)

    return ToolResult.success(
        "That's as far as I got.",
        f"screen_task '{goal}' hit the {ceiling}-step ceiling. "
        f"Done: {'; '.join(history) or 'nothing'}. Ask again to continue.",
    )


def _stopped(goal: str, history: list[str], why: str) -> ToolResult:
    """A cancelled run: what happened really happened, so this is a success."""
    return ToolResult.stopped(
        "Stopped.",
        f"screen_task '{goal}' {why}. Done before stopping: "
        f"{'; '.join(history) or 'nothing'}.",
    )


__all__ = [
    "CaptureError",
    "Frame",
    "VisionError",
    "ask_vision",
    "capture_screen",
    "close_vision_client",
    "frames_match",
    "keyboard_action",
    "mouse_action",
    "screen_size",
    "screen_task",
    "take_screenshot",
]
