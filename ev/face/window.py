"""The floating window the face lives in, and the pipe that drives it.

Three rules from `tools/overlay.py` carry over unchanged, because they are
about the same failure: something of E.V.'s on screen getting in the way of
the work E.V. is doing.

* **It never takes focus.** Everything E.V. types goes to the focused
  window. `WindowDoesNotAcceptFocus` and `WA_ShowWithoutActivating` keep the
  face out of it - on Windows that is `WS_EX_NOACTIVATE`, on X11 the window
  manager is bypassed altogether.
* **It can be made to never eat a click.** `EV_FACE_CLICKTHROUGH=true` adds
  `WindowTransparentForInput`. It is off by default, unlike the overlay's,
  because the face is small and sits in a corner rather than over the page,
  and being able to drag it out of the way is worth more than the corner.
* **Nothing here may break E.V.** The face is a separate process. If Qt is
  missing, the display refuses, or the window manager is hostile, the face
  is absent and the assistant carries on talking.

**Ubuntu and Wayland.** A Wayland compositor does not let a client choose
where its window goes or keep it above other windows - by design, and GNOME
offers no extension point a plain app can use. A face that cannot sit in a
corner or stay visible is not much of a face, so on a Wayland session it
runs through XWayland (`QT_QPA_PLATFORM=xcb`), where both still work, and
the window bypasses the window manager: no dock entry, no focus, visible on
every workspace. The one thing XWayland cannot give back is the pointer over
native Wayland windows, so cursor-following only works while the pointer is
over X11 windows or the face itself; the saccades keep the eyes alive
regardless. `EV_FACE_XWAYLAND=false` runs natively and accepts wherever the
compositor puts it.

**The pipe.** The core drives the face with JSON lines on stdin, read on a
thread and drained on the Qt thread, because widgets belong to the thread
that made them. End of file means the core has gone, and the face goes with
it: a face that outlives its assistant is a lie about whether anyone is
listening.

**Presence.** In `summoned` mode the face is out of sight until E.V. is
spoken to, slides in for the conversation and leaves when it lapses - the
core says when with `{"show": ...}`, because only the core knows whether
the wake phrase was heard. Whatever the mode, `ev.face.busy` can overrule
it: a full-screen video, a presentation or Do Not Disturb keeps the face
hidden however it was asked for. A hidden face is *hidden*, not transparent,
so it cannot eat a click while nobody can see it.

**Captions.** What the user said and what E.V. answered are drawn in a
bubble beside the face, in a window of its own that never takes input -
the face can be dragged, the caption must never be in the way.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading

import json
import time

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ev.face import busy, render
from ev.face.expression import Expression, apply_command

log = logging.getLogger("ev.face.window")

IS_LINUX = sys.platform.startswith("linux")
DRAG_THRESHOLD_PX = 4
# Asking X for the pointer is a server round trip, ~1ms. The gaze eases
# towards it anyway, so fifteen looks a second are indistinguishable from sixty.
CURSOR_POLL_S = 1 / 15


def prefer_xwayland(enabled: bool = True) -> bool:
    """Route Qt through XWayland on a Wayland session. Must run before QApplication."""
    if not (enabled and IS_LINUX):
        return False
    if os.environ.get("QT_QPA_PLATFORM"):
        return False  # the user chose; do not argue
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        return True
    return False


# Set by a snap (VS Code's, typically) for its own GTK, and inherited by
# everything started from its terminal. Qt's GTK platform theme follows
# GTK_PATH into the snap and loads libraries built against the snap's glibc,
# and the process dies with `undefined symbol: __libc_pthread_init` before a
# window exists. They are meaningless outside that snap.
_SNAP_LEAKS = (
    "GTK_PATH", "GTK_EXE_PREFIX", "GTK_IM_MODULE_FILE", "GIO_MODULE_DIR",
    "GDK_PIXBUF_MODULEDIR", "GDK_PIXBUF_MODULE_FILE", "GSETTINGS_SCHEMA_DIR", "LOCPATH",
)


def scrub_snap_env() -> list[str]:
    """Drop another snap's GTK paths before Qt loads. Returns what was removed."""
    if not IS_LINUX or not os.environ.get("SNAP") or os.environ.get("EV_FACE_KEEP_SNAP_ENV"):
        return []
    snap = os.environ["SNAP"]
    user = os.environ.get("SNAP_USER_DATA", snap)
    common = os.environ.get("SNAP_USER_COMMON", snap)
    removed = [k for k in _SNAP_LEAKS
               if any(os.environ.get(k, "").startswith(p) for p in (snap, user, common))]
    for key in removed:
        del os.environ[key]
    for key, value in list(os.environ.items()):
        if key.startswith("XDG_DATA_DIRS_") and key.endswith("_SNAP_ORIG"):
            os.environ["XDG_DATA_DIRS"] = value
            removed.append("XDG_DATA_DIRS")
            break
    return removed


def is_x11() -> bool:
    return QGuiApplication.platformName() == "xcb"


SLIDE_PX = 36
SLIDE_IN_MS = 280
SLIDE_OUT_MS = 200
BUSY_CHECK_MS = 500


class FaceWindow(QWidget):
    def __init__(self, expression: Expression, *, size: int = 240, position: str = "bottom-right",
                 margin: int = 28, fps: int = 60, idle_fps: int = 24,
                 clickthrough: bool = False, x11_bypass: bool = True,
                 presence: str = "always", captions: bool = True, caption_s: float = 7.0,
                 busy_poll_s: float = 2.0, hide_fullscreen: bool = True,
                 hide_video: bool = True, hide_dnd: bool = True) -> None:
        super().__init__()
        self.expression = expression
        self._presence = presence if presence in ("always", "summoned") else "always"
        self._summoned = self._presence == "always"
        self._shown = False
        self._busy = ""
        self._busy_seen = ""
        self._anim: QParallelAnimationGroup | None = None
        self._from_bottom = "top" not in position
        self._fps = max(10, fps)
        self._idle_fps = max(5, min(idle_fps, self._fps))
        self._frame = expression.tick(0.0)
        self._since_cursor = CURSOR_POLL_S
        self._press: QPoint | None = None
        self._origin: QPoint | None = None
        self._dragged = False

        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        if clickthrough:
            flags |= Qt.WindowType.WindowTransparentForInput
        if x11_bypass and is_x11():
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("E.V.")
        self.resize(size, int(size * 0.8))
        self._place(position, margin)

        self._home = self.pos()

        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._step)

        self.caption = CaptionWindow(size, caption_s, x11_bypass) if captions else None

        # The probes are subprocesses, so they run on a thread of their own
        # and only their answer crosses into Qt - a slow `gdbus` must cost a
        # stale answer, never a dropped frame.
        self._busy_flags = {"fullscreen": hide_fullscreen, "video": hide_video, "dnd": hide_dnd}
        if any(self._busy_flags.values()) and busy_poll_s > 0:
            threading.Thread(target=self._watch_busy, args=(busy_poll_s,),
                             name="ev-face-busy", daemon=True).start()
            self._busy_timer = QTimer(self)
            self._busy_timer.timeout.connect(lambda: self.set_busy(self._busy_seen))
            self._busy_timer.start(BUSY_CHECK_MS)

    # -- presence ---------------------------------------------------------

    def start(self) -> None:
        """Show the face if it should be visible now; otherwise stay out of sight."""
        self._apply_presence(animate=False)

    @property
    def shown(self) -> bool:
        return self._shown

    def summon(self, on: bool) -> None:
        """The core's word on whether E.V. is in a conversation."""
        self._summoned = bool(on) or self._presence == "always"
        self._apply_presence()

    def set_busy(self, reason: str) -> None:
        if reason == self._busy:
            return
        log.info("screen busy: %s", reason or "no longer")
        self._busy = reason
        self._apply_presence()

    def _watch_busy(self, interval: float) -> None:
        while True:
            try:
                self._busy_seen = busy.busy_reason(**self._busy_flags)
            except Exception as exc:  # a broken probe must not take the face down
                log.debug("busy probe failed: %s", exc)
                self._busy_seen = ""
            time.sleep(interval)

    def _apply_presence(self, animate: bool = True) -> None:
        want = self._summoned and not self._busy
        if want == self._shown and (want == self.isVisible()):
            return
        self._shown = want
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        offset = QPoint(0, SLIDE_PX if self._from_bottom else -SLIDE_PX)
        if want:
            self._clock.restart()
            self._timer.start(1000 // self._fps)
            if not animate:
                self.move(self._home)
                self.setWindowOpacity(1.0)
                self.show()
                return
            if not self.isVisible():
                self.setWindowOpacity(0.0)
                self.move(self._home + offset)
                self.show()
            self._animate(self._home, 1.0, SLIDE_IN_MS, QEasingCurve.Type.OutCubic)
            return
        if self.caption is not None:
            self.caption.dismiss()
        if not animate or not self.isVisible():
            self._finish_hide()
            return
        self._animate(self._home + offset, 0.0, SLIDE_OUT_MS, QEasingCurve.Type.InCubic,
                      self._finish_hide)

    def _animate(self, pos: QPoint, opacity: float, ms: int, curve, done=None) -> None:
        group = QParallelAnimationGroup(self)
        for prop, end in ((b"pos", pos), (b"windowOpacity", opacity)):
            anim = QPropertyAnimation(self, prop, group)
            anim.setDuration(ms)
            anim.setEndValue(end)
            anim.setEasingCurve(curve)
            group.addAnimation(anim)
        group.finished.connect(lambda: setattr(self, "_anim", None))
        if done is not None:
            group.finished.connect(done)
        self._anim = group
        group.start()

    def _finish_hide(self) -> None:
        if self._shown:
            return  # summoned again while it was leaving
        self.hide()
        self.move(self._home)
        # Hidden means asleep: no repaints, no cursor polls.
        self._timer.stop()

    # -- captions -----------------------------------------------------------

    def caption_heard(self, text: str) -> None:
        if self.caption is not None and self._shown:
            self.caption.heard(text, self._caption_anchor())

    def caption_said(self, text: str) -> None:
        if self.caption is not None and self._shown:
            self.caption.said(text, self._caption_anchor())

    def _caption_anchor(self) -> tuple[QRect, bool, bool]:
        rect = QRect(self._home, self.size())
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        mid = screen.availableGeometry().center() if screen is not None else rect.center()
        return rect, rect.center().y() > mid.y(), rect.center().x() > mid.x()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().moveEvent(event)
        if self.caption is not None and self.caption.isVisible() and self._anim is None:
            self.caption.follow(*self._caption_anchor())

    # -- placement --------------------------------------------------------

    def _place(self, position: str, margin: int) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = area.left() + margin if "left" in position else area.right() - self.width() - margin
        y = area.top() + margin if "top" in position else area.bottom() - self.height() - margin
        self.move(x, y)

    # -- animation --------------------------------------------------------

    def _step(self) -> None:
        dt = self._clock.restart() / 1000.0
        self._since_cursor += dt
        if self._since_cursor >= CURSOR_POLL_S and self.expression.active.track_cursor:
            self._since_cursor = 0.0
            self._track_cursor()
        self._frame = self.expression.tick(dt)
        if self.caption is not None and self.caption.isVisible():
            self.caption.set_accent(self._frame.face.color)
        interval = 1000 // (self._fps if self.expression.settling else self._idle_fps)
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        self.update()

    def _track_cursor(self) -> None:
        pos = QCursor.pos()
        centre = self.frameGeometry().center()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        span = screen.geometry()
        # Normalised against half the screen, so the eyes swing fully only
        # for something across the desk from them, not for the next icon over.
        self.expression.look_at((pos.x() - centre.x()) / max(1, span.width() / 2),
                                (pos.y() - centre.y()) / max(1, span.height() / 2))

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt's name
        painter = QPainter(self)
        try:
            render.paint(painter, QRectF(self.rect()), self._frame)
        finally:
            painter.end()

    # -- interaction ----------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._origin = self.pos()
            self._dragged = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is None or self._origin is None:
            return
        delta = event.globalPosition().toPoint() - self._press
        if delta.manhattanLength() > DRAG_THRESHOLD_PX:
            self._dragged = True
        if self._dragged:
            self.move(self._origin + delta)

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        if self._press is not None and not self._dragged:
            self.expression.event("poke")
        elif self._dragged:
            # Where the user put it is where it lives now, including the
            # next time it slides in.
            self._home = self.pos()
            screen = QGuiApplication.screenAt(self.frameGeometry().center())
            if screen is not None:
                self._from_bottom = self.frameGeometry().center().y() > screen.availableGeometry().center().y()
        self._press = None
        self._origin = None

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self.expression.set_mood("happy")


class CaptionWindow(QWidget):
    """What was heard and what was said, in a bubble beside the face.

    Never takes input and never takes focus: it exists to be read, and a
    caption that intercepted a click would be in the way of exactly the
    work E.V. was just asked to do.
    """

    PAD = 12
    GAP = 8
    MAX_CHARS = 420

    def __init__(self, face_size: int, hold_s: float, x11_bypass: bool = True) -> None:
        super().__init__()
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.NoDropShadowWindowHint)
        if x11_bypass and is_x11():
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("E.V. caption")
        self._width = max(240, min(440, int(face_size * 1.7)))
        self._hold_s = max(1.0, hold_s)
        self._heard = ""
        self._said = ""
        self._accent = QColor(92, 225, 255)
        self._said_font = QFont(self.font())
        self._said_font.setPointSizeF(max(10.0, self._said_font.pointSizeF() * 1.05))
        self._heard_font = QFont(self.font())
        self._heard_font.setItalic(True)
        self._heard_font.setPointSizeF(max(8.5, self._heard_font.pointSizeF() * 0.9))
        self._expire = QTimer(self)
        self._expire.setSingleShot(True)
        self._expire.timeout.connect(self.dismiss)
        self._fade: QPropertyAnimation | None = None

    # -- content ----------------------------------------------------------

    def heard(self, text: str, anchor) -> None:
        self._heard, self._said = _clip(text, self.MAX_CHARS), ""
        self._present(anchor)

    def said(self, text: str, anchor) -> None:
        self._said = _clip(text, self.MAX_CHARS)
        self._present(anchor)

    def set_accent(self, color: tuple[float, float, float]) -> None:
        accent = QColor(*(max(0, min(255, int(c))) for c in color))
        if accent != self._accent:
            self._accent = accent
            self.update()

    def dismiss(self) -> None:
        if not self.isVisible():
            return
        self._expire.stop()
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(260)
        fade.setEndValue(0.0)
        fade.finished.connect(self.hide)
        self._fade = fade
        fade.start()

    # -- layout -----------------------------------------------------------

    def _text_rects(self) -> tuple[QRect, QRect]:
        inner = self._width - 2 * self.PAD
        flags = int(Qt.TextFlag.TextWordWrap)
        heard = QFontMetrics(self._heard_font).boundingRect(QRect(0, 0, inner, 4000), flags, self._heard) \
            if self._heard else QRect()
        said = QFontMetrics(self._said_font).boundingRect(QRect(0, 0, inner, 4000), flags, self._said) \
            if self._said else QRect()
        return heard, said

    def _present(self, anchor) -> None:
        if not (self._heard or self._said):
            return
        heard, said = self._text_rects()
        gap = 6 if heard.height() and said.height() else 0
        self.resize(self._width, heard.height() + gap + said.height() + 2 * self.PAD)
        self.follow(*anchor)
        if self._fade is not None:
            self._fade.stop()
            self._fade = None
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self.update()
        # Long enough to read: a floor, plus about a word every 0.3s.
        words = len((self._heard + " " + self._said).split())
        self._expire.start(int(1000 * min(30.0, self._hold_s + 0.3 * words)))

    def follow(self, face: QRect, below_face: bool, at_right: bool) -> None:
        """Sit above a face near the bottom of the screen, below one near the top."""
        x = face.right() - self.width() + 1 if at_right else face.left()
        y = face.top() - self.height() - self.GAP if below_face else face.bottom() + self.GAP
        screen = QGuiApplication.screenAt(face.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt's name
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(box, 14, 14)
            p.fillPath(path, QColor(10, 14, 22, 236))
            edge = QColor(self._accent)
            edge.setAlpha(150)
            p.setPen(QPen(edge, 1.2))
            p.drawPath(path)

            heard, said = self._text_rects()
            flags = int(Qt.TextFlag.TextWordWrap)
            inner = self._width - 2 * self.PAD
            y = self.PAD
            if self._heard:
                p.setFont(self._heard_font)
                p.setPen(QColor(160, 170, 186))
                p.drawText(QRect(self.PAD, y, inner, heard.height()), flags, self._heard)
                y += heard.height() + (6 if self._said else 0)
            if self._said:
                p.setFont(self._said_font)
                p.setPen(QColor(236, 244, 252))
                p.drawText(QRect(self.PAD, y, inner, said.height()), flags, self._said)
        finally:
            p.end()


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def route_line(window: "FaceWindow | None", line: str) -> None:
    """The window's half of the protocol: presence and captions.

    `apply_command` owns moods and gaze and ignores these keys, so a line
    can carry both - `{"event": "speaking", "say": "..."}` - and each half
    is applied by the code that understands it.
    """
    if window is None:
        return
    text = line.strip()
    if not text.startswith("{"):
        return
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return
    if "show" in message:
        window.summon(bool(message["show"]))
    if message.get("heard"):
        window.caption_heard(str(message["heard"]))
    if message.get("say"):
        window.caption_said(str(message["say"]))


class StdinBridge:
    """Feeds JSON lines from stdin into the expression on the Qt thread."""

    def __init__(self, expression: Expression, on_eof, stream=None,
                 window: "FaceWindow | None" = None) -> None:
        self.expression = expression
        self.window = window
        self._on_eof = on_eof
        self._stream = stream or sys.stdin
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, name="ev-face-stdin", daemon=True).start()
        self._timer = QTimer()
        self._timer.timeout.connect(self.drain)
        self._timer.start(30)

    def _read(self) -> None:
        try:
            for line in self._stream:
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    def drain(self) -> None:
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                return
            if line is None or not apply_command(self.expression, line):
                self._timer.stop()
                self._on_eof()
                return
            route_line(self.window, line)
