"""The overlay that says E.V. has the screen, and the switch that takes it back.

An autonomous run moves the pointer, changes windows and types into things
while the user watches. Two things have to be true the whole time that is
happening, and neither of them is about correctness:

* **It must be obvious.** A pointer that moves on its own with no explanation
  is indistinguishable from a machine somebody else has taken over. So a band
  is drawn round the whole screen - a stack of rings fading outwards, with
  viewfinder brackets at the corners - and a panel names the errand, the
  round it is on, how long it has been going and the last thing it did.
  It is drawn well on purpose: what it is announcing is that this is
  deliberate, supervised and stoppable, and a badge that looks like a debug
  print says the opposite of all three.
* **It must be stoppable from anywhere.** The user will not be looking at
  E.V.'s terminal - they will be looking at whatever E.V. is driving. The
  kill switch is registered with the window manager, so it lands whatever has
  focus, including a full-screen application that owns every other keystroke.

Three constraints shape the drawing.

**The overlay must never take focus.** Everything E.V. types goes to the
focused window, so a HUD that stole focus would swallow the work it is
narrating. Both windows get `WS_EX_NOACTIVATE` and `WS_EX_TOOLWINDOW`: they
cannot be activated, and they stay out of Alt-Tab.

**The overlay must never eat a click.** The full-screen window is keyed out
with `-transparentcolor`, which on Windows makes those pixels both invisible
and click-through, and `WS_EX_TRANSPARENT` puts the rest of it out of the way
too. The panel is click-through by default for the same reason - a button
sitting over the page E.V. is about to click is a button that intercepts it.
`EV_AGENT_OVERLAY_CLICKTHROUGH=false` trades that back for a clickable STOP,
which is the right call on a machine with no hotkey to spare.

The panel is keyed too, which is what buys its rounded corners: the pixels
outside the rounded rectangle are the key colour, so they are not there at
all. Where keying is refused it falls back to a square panel rather than to
nothing, because the corners are decoration and the panel is not.

**Nothing here may break the run.** Tkinter may be missing, a display may be
absent, a window manager may refuse any of it. Every entry point swallows its
own failures and reports that it could not draw; the mission then runs
unannounced rather than not at all.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import re
import sys
import threading
import time
from typing import Any, Callable

import config

log = logging.getLogger("ev.tools.overlay")

IS_WINDOWS = sys.platform == "win32"

# Keyed out by the window manager, so the middle of the screen is both
# see-through and click-through. Deliberately a colour nothing else uses:
# every pixel of exactly this value disappears, so a near-black that also
# turned up in the badge would punch holes in it.
_KEY_COLOUR = "#010203"

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000

_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010


def _user32() -> Any:
    """user32, or None where there is no such thing."""
    if not IS_WINDOWS:
        return None
    try:
        return ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - not Windows
        log.debug("user32 unavailable: %s", exc)
        return None


def _window_handle(widget: Any) -> int:
    """The real top-level handle behind a Tk window.

    Tk sometimes wraps a toplevel in a frame of its own, in which case
    `winfo_id` is the child and the extended styles have to go on the
    parent. Asking for the parent and keeping it when there is one covers
    both shapes.
    """
    handle = int(widget.winfo_id())
    user32 = _user32()
    if user32 is None:
        return handle
    try:
        parent = int(user32.GetParent(handle))
    except Exception:  # pragma: no cover - defensive
        return handle
    return parent or handle


def _harden(widget: Any, click_through: bool) -> None:
    """Make a Tk window unfocusable, invisible to Alt-Tab, optionally inert."""
    user32 = _user32()
    if user32 is None:
        return
    try:
        handle = _window_handle(widget)
        getter = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        setter = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        style = int(getter(handle, _GWL_EXSTYLE))
        style |= _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
        if click_through:
            style |= _WS_EX_TRANSPARENT
        setter(handle, _GWL_EXSTYLE, style)
    except Exception as exc:  # pragma: no cover - cosmetic either way
        log.debug("Could not set overlay window styles: %s", exc)


def _raise_without_focus(widget: Any) -> None:
    """Put a window back on top without activating it.

    Re-asserted on every tick because the run keeps launching things, and a
    newly launched window arrives above everything - including the overlay
    that is supposed to be saying what is going on. `lift()` would do it and
    would also hand the overlay focus, which is the one thing it must never
    take.
    """
    user32 = _user32()
    if user32 is None:
        return
    try:
        user32.SetWindowPos(
            _window_handle(widget),
            _HWND_TOPMOST,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )
    except Exception:  # pragma: no cover - cosmetic
        pass


# ---------------------------------------------------------------------------
# The drawing
# ---------------------------------------------------------------------------
def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _rgb(colour: str) -> tuple[int, int, int]:
    """`"#ff3b30"` -> (255, 59, 48). Anything unreadable comes back grey."""
    text = (colour or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (128, 128, 128)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (128, 128, 128)


def _hex(rgb: tuple[float, float, float]) -> str:
    red, green, blue = (int(_clamp(part, 0, 255)) for part in rgb)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _mix(first: str, second: str, amount: float) -> str:
    """`amount` of the way from `first` to `second`.

    Tk has no alpha per shape - a canvas item is opaque or it is not there -
    so every soft edge in this file is a colour mixed towards whatever is
    behind it rather than a transparency. Mixing is also what keeps the
    palette coherent when the accent is overridden in `.env`: the dot, the
    fade and the brackets are all derived from that one colour.
    """
    ratio = _clamp(amount)
    start, end = _rgb(first), _rgb(second)
    return _hex(tuple(start[i] + (end[i] - start[i]) * ratio for i in range(3)))


# Near-black rather than black. A true black panel on a dark desktop has no
# edge at all, and the one thing this must never be is hard to find.
_PANEL = "#0d0f12"
_PANEL_EDGE = "#262a31"
_TEXT = "#f4f5f7"
_SUBTLE = "#a2a9b2"
_FAINT = "#6d747d"
_WARN = "#ffb02e"

_PAD = 18


def _round_points(x0: int, y0: int, x1: int, y1: int, radius: int) -> list[int]:
    """The polygon behind a rounded rectangle.

    A smoothed polygon whose corner points are trebled: the spline is pulled
    tight along the straight edges and rounds only where the points bunch up.
    It is the standard recipe, and it is here rather than four arcs and three
    rectangles because those cannot carry one outline round the whole shape.
    """
    r = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
    return [
        x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0,
        x1, y0 + r, x1, y1 - r, x1, y1 - r, x1, y1,
        x1 - r, y1, x0 + r, y1, x0 + r, y1, x0, y1,
        x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0,
    ]


def _round_rect(canvas: Any, x0: int, y0: int, x1: int, y1: int, radius: int, **kw: Any) -> int:
    return canvas.create_polygon(
        _round_points(x0, y0, x1, y1, radius), smooth=True, splinesteps=16, **kw
    )


class Overlay:
    """A frame round the screen and a panel naming the errand.

    Tk owns a thread of its own, because widgets may only be touched from the
    thread that created them and the mission is already running on a worker
    thread. Updates cross that boundary through a queue drained by a periodic
    callback, which is the only supported way to talk to a Tk loop from
    outside it.

    Everything is drawn on canvases rather than assembled out of widgets, and
    neither reason is taste: a canvas can have rounded corners and a Tk frame
    cannot, and one canvas item's colour can be changed ten times a second
    without the layout being done again.
    """

    def __init__(self, goal: str, on_kill: Callable[[], None] | None = None) -> None:
        self.goal = goal
        self._on_kill = on_kill
        self._updates: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._closing = threading.Event()
        self._ready = threading.Event()
        self._drawing = False
        self._thread: threading.Thread | None = None
        # Everything the panel shows, kept on this side of the queue so the
        # Tk thread never has to ask the mission anything.
        self._status = "Starting up."
        self._round = 0
        self._rounds = 0
        self._stopping = False
        self._started = time.monotonic()
        self._brackets: list[int] = []
        self._glow_canvas: Any = None
        self._badge_canvas: Any = None
        self._badge_width = 0
        self._badge_height = 0
        self._bar_y = 0

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Draw it. False means there is no overlay, which is survivable."""
        if not config.AGENT_OVERLAY_ENABLED:
            return False
        self._started = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="ev-overlay", daemon=True
        )
        self._thread.start()
        # Bounded: a Tk that never comes up must not hold the mission at the
        # starting line.
        self._ready.wait(timeout=config.AGENT_OVERLAY_START_S)
        return self._drawing

    def close(self) -> None:
        self._closing.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._drawing = False

    # -- updates ----------------------------------------------------------
    def note(self, status: str) -> None:
        """Say what is happening now. Safe from any thread, never raises."""
        if self._drawing:
            self._updates.put(("status", status))

    def set_goal(self, goal: str) -> None:
        self.goal = goal
        if self._drawing:
            self._updates.put(("goal", goal))

    def stopping(self) -> None:
        """The kill switch has been pressed; show it in the loudest way left."""
        if self._drawing:
            self._updates.put(("stopping", ""))

    # -- the Tk thread ----------------------------------------------------
    def _run(self) -> None:
        root = None
        try:
            root = self._build()
        except Exception as exc:
            log.info("No screen overlay this run: %s", exc)
        finally:
            self._ready.set()
        if root is None:
            return
        try:
            root.mainloop()
        except Exception as exc:  # pragma: no cover - Tk teardown races
            log.debug("Overlay loop ended: %s", exc)
        finally:
            self._drawing = False
            try:
                root.destroy()
            except Exception:
                pass

    def _build(self) -> Any:
        import tkinter as tk

        self.accent = config.AGENT_OVERLAY_COLOUR
        click_through = config.AGENT_OVERLAY_CLICKTHROUGH

        # The panel is the half that must exist. The full-screen frame is a
        # bonus that needs colour keying to be click-through, and a platform
        # that cannot do that gets the panel alone rather than a sheet of
        # glass laid over the work.
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        glow = self._build_glow(tk, root)
        badge = self._build_badge(tk, root, click_through)

        self._drawing = True
        self._fade_in(badge, config.AGENT_OVERLAY_OPACITY)
        root.after(
            config.AGENT_OVERLAY_TICK_MS, lambda: self._pump(root, badge, glow)
        )
        return root

    # -- the frame round the screen ---------------------------------------
    def _build_glow(self, tk: Any, root: Any) -> Any:
        """The band round the whole screen, or None where it cannot be safe.

        Safe here means click-through. Without colour keying the window is a
        transparent sheet that still swallows every click, which would stop
        the very automation it is announcing - so it is not drawn at all
        rather than drawn dangerously.

        The band is a stack of one-pixel rings mixed from the accent towards
        the key colour, so it fades outwards instead of ending in a line, and
        the whole window is then made translucent. A single hard rectangle
        reads as an error dialog; this reads as a viewfinder, which is what
        it actually is.
        """
        try:
            glow = tk.Toplevel(root)
            glow.overrideredirect(True)
            glow.attributes("-topmost", True)
            glow.attributes("-transparentcolor", _KEY_COLOUR)
            glow.configure(bg=_KEY_COLOUR)
            width = glow.winfo_screenwidth()
            height = glow.winfo_screenheight()
            glow.geometry(f"{width}x{height}+0+0")
            canvas = tk.Canvas(
                glow, width=width, height=height, bg=_KEY_COLOUR,
                highlightthickness=0, bd=0,
            )
            canvas.pack(fill="both", expand=True)

            layers = max(1, config.AGENT_OVERLAY_GLOW_LAYERS)
            # A crisp line at the very edge, then a halo falling away from
            # it. The line is what the eye catches; the halo is what stops
            # the line looking like a window border somebody drew by hand.
            edge = max(1, config.AGENT_OVERLAY_BORDER_PX // 3)
            for ring in range(layers):
                fade = 0.0 if ring < edge else ((ring - edge) / max(1, layers - edge)) ** 0.6
                canvas.create_rectangle(
                    ring + 0.5, ring + 0.5, width - ring - 0.5, height - ring - 0.5,
                    outline=_mix(self.accent, _KEY_COLOUR, fade), width=1,
                )

            # Viewfinder brackets: brighter, thicker, and only at the
            # corners, so the eye reads a deliberate instrument rather than a
            # window that has gone wrong.
            arm = max(12, config.AGENT_OVERLAY_BRACKET_PX)
            thick = max(4, config.AGENT_OVERLAY_BORDER_PX)
            # Lifted towards white rather than drawn in the accent: a bracket
            # the same colour as the band it sits on is a bracket nobody can
            # see, and the corners are the whole of what makes this read as
            # an instrument.
            self._bracket_colour = _mix(self.accent, "#ffffff", 0.38)
            # On the edge rather than inside the halo: a bracket set in from
            # it reads as a second border, which is one border too many.
            inset = 0
            for x0, y0, dx, dy in (
                (0, 0, 1, 1), (width, 0, -1, 1),
                (0, height, 1, -1), (width, height, -1, -1),
            ):
                x = x0 + dx * inset
                y = y0 + dy * inset
                self._brackets.append(
                    canvas.create_line(
                        x, y + dy * arm, x, y, x + dx * arm, y,
                        fill=self._bracket_colour, width=thick,
                        capstyle="projecting",
                    )
                )
            self._glow_canvas = canvas

            try:
                glow.attributes(
                    "-alpha", _clamp(config.AGENT_OVERLAY_GLOW_ALPHA, 0.15, 1.0)
                )
            except Exception:  # pragma: no cover - a window manager may refuse
                pass
            # Always inert, whatever the panel is set to: this one covers
            # every pixel of the screen, so a click it ate could be any click.
            _harden(glow, click_through=True)
            return glow
        except Exception as exc:
            log.debug("No full-screen frame: %s", exc)
            self._glow_canvas = None
            self._brackets = []
            return None

    # -- the panel --------------------------------------------------------
    def _build_badge(self, tk: Any, root: Any, click_through: bool) -> Any:
        """The panel: what the errand is, where it has got to, how to stop it."""
        badge = tk.Toplevel(root)
        badge.overrideredirect(True)
        badge.attributes("-topmost", True)
        try:
            badge.attributes("-alpha", 0.0)  # faded in once it has been placed
        except Exception:
            pass
        # Keyed out for the same reason the frame is: without it the rounded
        # corners are four grey squares.
        keyed = True
        try:
            badge.attributes("-transparentcolor", _KEY_COLOUR)
        except Exception:
            keyed = False
        background = _KEY_COLOUR if keyed else _PANEL
        badge.configure(bg=background)

        width = max(360, config.AGENT_OVERLAY_WIDTH)
        canvas = tk.Canvas(
            badge, width=width, height=140, bg=background,
            highlightthickness=0, bd=0,
        )
        canvas.pack(fill="both", expand=True)
        self._badge_canvas = canvas
        self._badge_width = width

        radius = max(0, config.AGENT_OVERLAY_CORNER_PX)
        self._panel = _round_rect(
            canvas, 1, 1, width - 1, 138, radius,
            fill=_PANEL, outline=_PANEL_EDGE, width=1,
        )
        # One accent hairline along the top of the panel. It is the whole of
        # the branding, and it survives being looked at for ten minutes.
        self._rule = canvas.create_line(
            radius, 3, width - radius, 3, fill=self.accent, width=2,
        )
        self._dot = canvas.create_oval(
            _PAD, 25, _PAD + 9, 34, fill=self.accent, outline="",
        )
        self._title = canvas.create_text(
            _PAD + 18, 29, anchor="w", text="E.V. HAS CONTROL",
            fill=_TEXT, font=("Segoe UI Semibold", 10),
        )
        self._counter = canvas.create_text(
            width - _PAD, 29, anchor="e", text="", fill=_FAINT,
            font=("Consolas", 9),
        )
        self._goal_text = canvas.create_text(
            _PAD, 48, anchor="nw", text=self.goal, fill=_TEXT,
            font=("Segoe UI", 10), width=width - _PAD * 2, justify="left",
        )
        self._status_text = canvas.create_text(
            _PAD, 76, anchor="nw", text=self._status, fill=_SUBTLE,
            font=("Segoe UI", 9), width=width - _PAD * 2, justify="left",
        )
        self._track = canvas.create_line(
            _PAD, 104, width - _PAD, 104, fill=_PANEL_EDGE, width=3,
            capstyle="round",
        )
        self._progress = canvas.create_line(
            _PAD, 104, _PAD + 3, 104, fill=self.accent, width=3, capstyle="round",
        )
        self._hint = canvas.create_text(
            _PAD, 118, anchor="nw", text=self._kill_hint(), fill=_FAINT,
            font=("Segoe UI", 8),
        )

        self._stop_shape = None
        self._stop_label = None
        if not click_through:
            # Only where the user has said they would rather have a button
            # than an inert panel. A STOP sitting over the page E.V. is about
            # to click is a button that intercepts that click.
            self._stop_shape = _round_rect(
                canvas, width - _PAD - 76, 110, width - _PAD, 132, 11,
                fill=self.accent, outline="",
            )
            self._stop_label = canvas.create_text(
                width - _PAD - 38, 121, text="STOP", fill="#ffffff",
                font=("Segoe UI Semibold", 9),
            )
            for item in (self._stop_shape, self._stop_label):
                canvas.tag_bind(item, "<Button-1>", lambda _event: self._fire())

        self._reflow()
        self._place(badge)
        _harden(badge, click_through)
        return badge

    def _reflow(self) -> None:
        """Fit the panel to however much text it is carrying.

        The goal is one line for "open my email" and three for a real errand,
        and a panel sized for one of those looks wrong holding the other.
        Everything below the goal is moved by the difference rather than laid
        out again from scratch, which is both steadier and cheaper.
        """
        canvas = self._badge_canvas
        width = self._badge_width

        goal_box = canvas.bbox(self._goal_text)
        status_top = (goal_box[3] if goal_box else 66) + 8
        canvas.coords(self._status_text, _PAD, status_top)
        status_box = canvas.bbox(self._status_text)
        status_bottom = status_box[3] if status_box else status_top + 16

        self._bar_y = int(status_bottom + 15)
        canvas.coords(self._track, _PAD, self._bar_y, width - _PAD, self._bar_y)
        self._draw_progress()
        canvas.coords(self._hint, _PAD, self._bar_y + 10)
        hint_box = canvas.bbox(self._hint)
        height = int((hint_box[3] if hint_box else self._bar_y + 26) + _PAD - 6)

        if self._stop_shape is not None:
            # On the hint's own line rather than under it. The panel is a
            # strip across the top of somebody's work: every row of it that
            # says nothing is a row of their screen it did not need to take.
            top = self._bar_y + 8
            canvas.coords(
                self._stop_shape,
                *_round_points(width - _PAD - 76, top, width - _PAD, top + 24, 11),
            )
            canvas.coords(self._stop_label, width - _PAD - 38, top + 12)
            height = max(height, top + 24 + _PAD - 6)

        canvas.coords(
            self._panel,
            *_round_points(1, 1, width - 1, height - 1, config.AGENT_OVERLAY_CORNER_PX),
        )
        canvas.configure(height=height)
        self._badge_height = height

    def _draw_progress(self) -> None:
        """The round counter as a bar, because a run is long enough to wonder."""
        canvas = self._badge_canvas
        span = self._badge_width - _PAD * 2
        fraction = _clamp(self._round / float(self._rounds)) if self._rounds else 0.0
        end = _PAD + max(3, int(span * fraction))
        canvas.coords(self._progress, _PAD, self._bar_y, end, self._bar_y)
        canvas.itemconfigure(
            self._progress, fill=_WARN if self._stopping else self.accent
        )

    def _place(self, badge: Any) -> None:
        """Centred across, and out of the way down."""
        badge.update_idletasks()
        screen_w = badge.winfo_screenwidth()
        screen_h = badge.winfo_screenheight()
        margin = max(4, config.AGENT_OVERLAY_MARGIN_PX)
        x = max(0, (screen_w - self._badge_width) // 2)
        y = margin
        if config.AGENT_OVERLAY_POSITION.startswith("bottom"):
            y = max(margin, screen_h - self._badge_height - margin - 48)
        badge.geometry(f"{self._badge_width}x{self._badge_height}+{x}+{y}")

    def _fade_in(self, badge: Any, target: float) -> None:
        """Arrive rather than appear.

        Timed off the wall clock rather than a step count, so a slow machine
        fades faster instead of taking a second and a half to show a warning.
        """
        steps = 8
        span = max(0.0, config.AGENT_OVERLAY_FADE_S)
        if span <= 0:
            try:
                badge.attributes("-alpha", target)
            except Exception:
                pass
            return
        delay = max(1, int(span * 1000 / steps))

        def step(index: int) -> None:
            try:
                badge.attributes("-alpha", target * (index / steps))
            except Exception:
                return
            if index < steps and not self._closing.is_set():
                badge.after(delay, lambda: step(index + 1))

        step(1)

    def _kill_hint(self) -> str:
        hotkey = (config.AGENT_KILL_HOTKEY or "").strip()
        parts = []
        if hotkey and config.AGENT_HOTKEY_ENABLED:
            parts.append(hotkey.upper())
        parts.append("say “stop everything”")
        # The STOP button is not listed when it is drawn: it is right there,
        # in the accent colour, saying the word itself.
        return "Kill switch:  " + "   ·   ".join(parts)

    # -- the tick ---------------------------------------------------------
    _ROUND = re.compile(r"round\s+(\d+)\s+of\s+(\d+)", re.I)

    def _apply(self) -> bool:
        """Drain the queue. True if the panel has to be laid out again."""
        canvas = self._badge_canvas
        dirty = False
        while True:
            try:
                kind, value = self._updates.get_nowait()
            except queue.Empty:
                return dirty
            except Exception:  # pragma: no cover - a dropped update is cosmetic
                return dirty

            if kind == "status":
                # The round counter rides in the status line both callers
                # already send, so neither had to learn a second method to
                # keep the progress bar honest.
                match = self._ROUND.search(value)
                if match:
                    self._round, self._rounds = int(match.group(1)), int(match.group(2))
                    value = self._ROUND.sub("", value, count=1).strip(" :-–")
                self._status = value or self._status
                canvas.itemconfigure(self._status_text, text=self._status)
                dirty = True
            elif kind == "goal":
                self.goal = value
                canvas.itemconfigure(self._goal_text, text=value)
                dirty = True
            elif kind == "stopping":
                self._stopping = True
                canvas.itemconfigure(self._title, text="E.V. IS STOPPING")
                canvas.itemconfigure(self._rule, fill=_WARN)
                canvas.itemconfigure(self._dot, fill=_WARN)
                dirty = True

    def _pump(self, root: Any, badge: Any, glow: Any) -> None:
        try:
            if self._apply():
                self._reflow()
                self._place(badge)
            self._tick_chrome()
        except Exception as exc:  # pragma: no cover - a bad frame is cosmetic
            log.debug("Overlay tick failed: %s", exc)

        if self._closing.is_set():
            root.quit()
            return

        for widget in (glow, badge):
            if widget is not None:
                _raise_without_focus(widget)
        root.after(
            config.AGENT_OVERLAY_TICK_MS, lambda: self._pump(root, badge, glow)
        )

    def _tick_chrome(self) -> None:
        """The clock, the counter and the breathing dot.

        All three are one-item text or colour changes. Nothing here lays
        anything out, which is what makes it safe to run ten times a second
        on a machine that is also driving a browser.
        """
        canvas = self._badge_canvas
        elapsed = int(time.monotonic() - self._started)
        counter = f"{elapsed // 60:d}:{elapsed % 60:02d}"
        if self._rounds:
            counter = f"ROUND {self._round}/{self._rounds}     {counter}"
        canvas.itemconfigure(self._counter, text=counter)

        if not config.AGENT_OVERLAY_PULSE:
            return
        # A triangle wave rather than a sine: indistinguishable on a nine
        # pixel dot, and it needs no import.
        period = 1.6
        depth = abs(((time.monotonic() - self._started) % period / period) * 2 - 1)
        live = _WARN if self._stopping else self.accent
        canvas.itemconfigure(self._dot, fill=_mix(live, _PANEL, 0.62 * depth))
        corner = _WARN if self._stopping else getattr(self, "_bracket_colour", live)
        for item in self._brackets:
            try:
                self._glow_canvas.itemconfigure(
                    item, fill=_mix(corner, _KEY_COLOUR, 0.4 * depth)
                )
            except Exception:  # pragma: no cover - the frame may be absent
                break

    def _fire(self) -> None:
        if self._on_kill is not None:
            try:
                self._on_kill()
            except Exception as exc:  # pragma: no cover - the stop still stands
                log.debug("Kill callback raised: %s", exc)


# ---------------------------------------------------------------------------
# The hotkey
# ---------------------------------------------------------------------------
_MODIFIERS = {
    "alt": 0x0001,
    "ctrl": 0x0002,
    "control": 0x0002,
    "shift": 0x0004,
    "win": 0x0008,
    "super": 0x0008,
    "meta": 0x0008,
}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012

_NAMED_KEYS = {
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "tab": 0x09, "enter": 0x0D,
    "return": 0x0D, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "pause": 0x13, "scrolllock": 0x91,
}
for _n in range(1, 13):
    _NAMED_KEYS[f"f{_n}"] = 0x6F + _n


def parse_hotkey(combo: str) -> tuple[int, int] | None:
    """`"ctrl+alt+q"` -> (modifier mask, virtual key), or None if unreadable.

    A hotkey with no modifier is refused. `RegisterHotKey` would happily take
    a bare letter and then swallow that letter system-wide for as long as the
    run lasts, which turns a safety feature into a broken keyboard.
    """
    parts = [part.strip().lower() for part in (combo or "").split("+") if part.strip()]
    if len(parts) < 2:
        return None
    mods = 0
    for part in parts[:-1]:
        bit = _MODIFIERS.get(part)
        if bit is None:
            return None
        mods |= bit
    key = parts[-1]
    if key in _NAMED_KEYS:
        return mods | _MOD_NOREPEAT, _NAMED_KEYS[key]
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        return mods | _MOD_NOREPEAT, ord(key.upper())
    return None


class KillSwitch:
    """A global hotkey meaning stop, wherever the user happens to be looking.

    Registered with the window manager rather than read from a keyboard hook,
    which matters for both halves of the job: it needs no extra dependency,
    and it reaches E.V. even when a full-screen application owns every other
    keystroke on the machine.
    """

    def __init__(self, combo: str, on_fire: Callable[[], None]) -> None:
        self.combo = combo
        self._on_fire = on_fire
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._registered = threading.Event()
        self._started = threading.Event()

    def start(self) -> bool:
        """Register it. False means the user has the other exits, not none."""
        if not config.AGENT_HOTKEY_ENABLED or not IS_WINDOWS:
            return False
        if parse_hotkey(self.combo) is None:
            log.warning("Unusable kill-switch hotkey %r; not registered", self.combo)
            return False
        self._thread = threading.Thread(
            target=self._run, name="ev-killswitch", daemon=True
        )
        self._thread.start()
        self._started.wait(timeout=2.0)
        return self._registered.is_set()

    def stop(self) -> None:
        user32 = _user32()
        if user32 is None or not self._thread_id:
            return
        try:
            # The message loop is blocked in GetMessage, so it is woken with a
            # message rather than with a flag it would never get round to
            # reading.
            user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        except Exception:  # pragma: no cover - the thread is a daemon anyway
            pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)

    def _run(self) -> None:  # pragma: no cover - needs a real message queue
        user32 = _user32()
        parsed = parse_hotkey(self.combo)
        if user32 is None or parsed is None:
            self._started.set()
            return
        mods, key = parsed
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())  # type: ignore[attr-defined]
        hotkey_id = 0xEF01
        try:
            if not user32.RegisterHotKey(None, hotkey_id, mods, key):
                log.warning(
                    "Kill-switch hotkey %s is already taken by something else",
                    self.combo,
                )
                return
            self._registered.set()
        except Exception as exc:
            log.warning("Could not register the kill switch: %s", exc)
            return
        finally:
            self._started.set()

        try:
            message = ctypes.wintypes.MSG()  # type: ignore[attr-defined]
            while True:
                got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if got in (0, -1):  # WM_QUIT, or the queue broke
                    return
                if message.message == _WM_HOTKEY:
                    log.warning("Kill switch pressed (%s)", self.combo)
                    try:
                        self._on_fire()
                    except Exception as exc:
                        log.debug("Kill callback raised: %s", exc)
                    return
        finally:
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass


# `ctypes.wintypes` is imported for MSG alone, and only on Windows: importing
# it elsewhere raises, and the message loop above is all that needs it.
if IS_WINDOWS:  # pragma: no cover - platform specific
    import ctypes.wintypes  # noqa: E402,F401


class Takeover:
    """Overlay plus kill switch, as one thing a run turns on and off.

    The stop is idempotent and one-way. Both routes into it - the hotkey and
    the STOP button - land on the same latch, so a user who hits both does not
    cancel twice, and a run that is already stopping is not stopped again.
    """

    def __init__(self, goal: str, on_kill: Callable[[], None] | None = None) -> None:
        self.goal = goal
        self._on_kill = on_kill
        self._killed = threading.Event()
        self._overlay = Overlay(goal, self._fire)
        self._switch = KillSwitch(config.AGENT_KILL_HOTKEY, self._fire)
        self.drawing = False
        self.hotkey_live = False

    def __enter__(self) -> "Takeover":
        self.drawing = self._overlay.start()
        self.hotkey_live = self._switch.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._switch.stop()
        self._overlay.close()

    @property
    def killed(self) -> bool:
        return self._killed.is_set()

    def note(self, status: str) -> None:
        self._overlay.note(status)

    def set_goal(self, goal: str) -> None:
        self.goal = goal
        self._overlay.set_goal(goal)

    def _fire(self) -> None:
        if self._killed.is_set():
            return
        self._killed.set()
        # The panel says so in its own right - title, colour and bar all
        # change - as well as in the status line. A run that is stopping
        # while the pointer is still finishing a click is exactly when
        # "is it listening to me?" gets asked.
        self._overlay.stopping()
        self._overlay.note("Stopping - kill switch pressed.")
        if self._on_kill is not None:
            self._on_kill()


__all__ = ["KillSwitch", "Overlay", "Takeover", "parse_hotkey"]
