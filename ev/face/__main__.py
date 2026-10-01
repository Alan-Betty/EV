"""python -m ev.face - the face as a process of its own.

    python -m ev.face                  # live face, idle, follows the cursor
    python -m ev.face --demo           # walks through every mood
    python -m ev.face --stdin          # driven by JSON lines (how the core runs it)
    python -m ev.face --sheet out.png  # every mood on one image, no window needed
    python -m ev.face --mood thinking  # start in a given mood

`python ev_core.py` starts it by itself (EV_FACE_ENABLED), so running it by
hand is only for working on the face.

stdin protocol, one JSON object per line - every key optional:
    {"event": "thinking"}  {"mood": "happy"}  {"look": [x, y]}
    {"show": true}         summon / dismiss (EV_FACE_PRESENCE=summoned)
    {"heard": "..."}       what the user said, in the caption bubble
    {"say": "..."}         what E.V. answered
    {"quit": true}

Click the face to poke it, double-click to make it happy, drag to move it.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
from ev.face.expression import Expression, load_library, still_frame  # noqa: E402

log = logging.getLogger("ev.face")

SHEET_COLUMNS = 4


def _qt():
    try:
        from PySide6 import QtWidgets  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "The face needs PySide6. In the project venv:  pip install PySide6-Essentials\n"
            "(On Ubuntu the distro package is python3-pyside6.qtwidgets, but the venv will\n"
            " not see it unless it was created with --system-site-packages.)\n")
        raise SystemExit(2)


def render_sheet(path: str, size: int) -> None:
    """Draw every mood at rest into one PNG. Works headless."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _qt()
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter

    from ev.face import render

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    library = load_library()
    names = list(library.moods)
    w, h = size, int(size * 0.8)
    rows = (len(names) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
    image = QImage(w * SHEET_COLUMNS, h * rows, QImage.Format.Format_ARGB32_Premultiplied)
    # A mid-grey desktop rather than transparent, so the glow is visible in a viewer.
    image.fill(QColor(44, 48, 56))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i, name in enumerate(names):
            x, y = (i % SHEET_COLUMNS) * w, (i // SHEET_COLUMNS) * h
            render.paint(painter, QRectF(x, y, w, h), still_frame(library.moods[name]))
            painter.setPen(QColor(200, 205, 215))
            painter.drawText(QRectF(x, y + h - 18, w, 16), Qt.AlignmentFlag.AlignCenter, name)
    finally:
        painter.end()
    image.save(path)
    print(f"{len(names)} moods -> {path}")


def run(args: argparse.Namespace) -> int:
    from ev.face.window import prefer_xwayland, scrub_snap_env

    scrubbed = scrub_snap_env()
    if scrubbed:
        log.info("ignoring snap environment: %s", ", ".join(scrubbed))
    prefer_xwayland(config.FACE_XWAYLAND)
    _qt()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from ev.face.window import FaceWindow, StdinBridge

    app = QApplication(sys.argv[:1])
    app.setApplicationName("ev-face")
    app.setQuitOnLastWindowClosed(True)

    expression = Expression(load_library(), mood=args.mood or config.FACE_MOOD)
    window = FaceWindow(
        expression,
        size=config.FACE_SIZE_PX,
        position=config.FACE_POSITION,
        margin=config.FACE_MARGIN_PX,
        fps=config.FACE_FPS,
        idle_fps=config.FACE_IDLE_FPS,
        clickthrough=config.FACE_CLICKTHROUGH,
        x11_bypass=config.FACE_X11_BYPASS,
        # Only the core can summon it, so without a pipe it is always up.
        presence=config.FACE_PRESENCE if args.stdin else "always",
        captions=config.FACE_CAPTIONS,
        caption_s=config.FACE_CAPTION_S,
        busy_poll_s=config.FACE_BUSY_POLL_S,
        hide_fullscreen=config.FACE_HIDE_FULLSCREEN,
        hide_video=config.FACE_HIDE_VIDEO,
        hide_dnd=config.FACE_HIDE_DND,
    )
    window.start()
    log.info("face up on %s", app.platformName())

    keep: list[object] = []
    if args.stdin:
        keep.append(StdinBridge(expression, on_eof=app.quit, window=window))
    if args.demo:
        names = list(expression.library.moods)
        state = {"i": 0}

        def advance() -> None:
            name = names[state["i"] % len(names)]
            state["i"] += 1
            expression.set_mood(name)
            print(f"mood: {name}", flush=True)

        timer = QTimer()
        timer.timeout.connect(advance)
        timer.start(2600)
        advance()
        keep.append(timer)

    # Qt's loop does not return to Python on its own, so Ctrl+C would wait
    # for the next event. A slow no-op timer gives the signal a way in.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(250)
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ev.face", description="E.V.'s floating face")
    parser.add_argument("--demo", action="store_true", help="cycle through every mood")
    parser.add_argument("--stdin", action="store_true", help="read JSON-line commands from stdin")
    parser.add_argument("--mood", help="mood to start in")
    parser.add_argument("--sheet", metavar="PNG", help="render every mood to an image and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(name)s: %(message)s")
    if args.sheet:
        render_sheet(args.sheet, config.FACE_SIZE_PX)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
