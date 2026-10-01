"""Draw one `Frame` with QPainter. No state, no timing, no decisions.

The face is a small dark screen with two glowing eyes on it - a robot whose
whole expressive range is its eyes, the way EMO's or EVE's is. Keeping it to
eyes is a constraint that pays: nothing on it needs a label, it reads at
240px across, and every mood is a handful of numbers rather than artwork.

The lids are not drawn, they are *subtracted*. An eye is a rounded rectangle;
an upper lid is a slanted half-plane cut out of it, and the smile-eye is an
ellipse cut out of the bottom - which is exactly the crescent a happy robot
makes. Composing shapes this way means every mood is the same four
operations with different numbers, and a mood half way to another is still
a sensible shape, which is what makes transitions free.

Soft edges are layered rather than blurred: a radial bloom behind each eye
and one faint, slightly larger copy of it read as a glow at a fraction of the cost of
a blur pass. Wide stroked pens did the same job first and cost most of a
frame - 8.5ms of it at 240px - which at 60fps is a third of a CPU core spent
on a face in the corner. The screen behind the eyes is cached outright.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QImage,
    QPolygonF,
    QRadialGradient,
    QTransform,
)

from ev.face.expression import Eye, Frame

SCREEN_INSET = 0.11      # room round the screen for the glow and the ring
SCREEN_RADIUS = 0.3      # of screen height
EYE_W = 0.2              # of screen width
EYE_H = 0.44             # of screen height
EYE_SPACING = 0.205      # eye centre from screen centre, of screen width
GAZE_TRAVEL = 0.085      # how far the eye pair moves at full gaze
PUPIL_TRAVEL = 0.16      # how far the core moves inside the eye
SCANLINE_PX = 3


def _rgba(color: tuple[float, float, float], alpha: float) -> QColor:
    r, g, b = (max(0, min(255, int(c))) for c in color)
    return QColor(r, g, b, max(0, min(255, int(alpha * 255))))


def _mix(color: tuple[float, float, float], towards: tuple[float, float, float], amount: float):
    return tuple(a + (b - a) * amount for a, b in zip(color, towards))


def screen_rect(rect: QRectF) -> QRectF:
    inset = SCREEN_INSET * min(rect.width(), rect.height())
    return rect.adjusted(inset, inset, -inset, -inset)


def eye_path(eye: Eye, side: int, base_w: float, base_h: float) -> QPainterPath:
    """One eye in its own coordinates, centred on the origin, lids cut out.

    `side` is -1 for the viewer's left eye and +1 for the right, which is
    what turns the outer-relative angles in the JSON into real ones.
    """
    w = base_w * eye.width
    h = max(base_h * eye.height * eye.open, 1.6)
    radius = eye.roundness * min(w, h) / 2.0
    path = QPainterPath()
    path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), radius, radius)

    if eye.lid_top > 0.001:
        y0 = -h / 2 + eye.lid_top * h
        slope = math.tan(math.radians(eye.lid_angle)) * -side

        def lid_y(x: float) -> float:
            return y0 + slope * x

        lid = QPolygonF([
            QPointF(-w, lid_y(-w)), QPointF(w, lid_y(w)),
            QPointF(w, -h * 3), QPointF(-w, -h * 3),
        ])
        cut = QPainterPath()
        cut.addPolygon(lid)
        path = path.subtracted(cut)

    if eye.lid_bottom > 0.001:
        ew, eh = w * 1.6, h * 1.3
        cy = h / 2 + eh / 2 - eye.lid_bottom * h * 1.05
        cut = QPainterPath()
        cut.addEllipse(QPointF(0, cy), ew / 2, eh / 2)
        path = path.subtracted(cut)
    return path


def _body(screen: QRectF) -> QPainterPath:
    radius = SCREEN_RADIUS * screen.height()
    body = QPainterPath()
    body.addRoundedRect(screen, radius, radius)
    return body


# The screen is the expensive part of a frame - wide antialiased strokes and
# a scanline per three pixels - and almost none of it changes between frames.
# So it is drawn once per (size, colour, glow) into an image and blitted.
# Colour and glow are quantised for the key: a pulse sweeps glow through a
# dozen steps and then hits the cache forever, where an exact key would miss
# on every frame of every transition.
_SCREEN_CACHE: dict[tuple, QImage] = {}
_SCREEN_CACHE_MAX = 64


def _screen_layer(w: float, h: float, dpr: float, color, glow: float) -> QImage:
    color_q = tuple(int(c) // 6 * 6 for c in color)
    glow_q = round(glow * 25) / 25
    key = (round(w), round(h), dpr, color_q, glow_q)
    image = _SCREEN_CACHE.get(key)
    if image is None:
        if len(_SCREEN_CACHE) >= _SCREEN_CACHE_MAX:
            _SCREEN_CACHE.clear()
        image = QImage(max(1, round(w * dpr)), max(1, round(h * dpr)),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            _draw_screen(painter, screen_rect(QRectF(0, 0, w, h)), color_q, glow_q)
        finally:
            painter.end()
        _SCREEN_CACHE[key] = image
    return image


def _draw_screen(p: QPainter, screen: QRectF, color, glow: float) -> None:
    body = _body(screen)

    # Ambient halo first, so the screen sits on it rather than under it.
    p.setBrush(Qt.BrushStyle.NoBrush)
    for width, alpha in ((16.0, 0.035), (9.0, 0.06), (4.0, 0.09)):
        p.setPen(QPen(_rgba(color, alpha * glow), width))
        p.drawPath(body)

    fill = QLinearGradient(screen.topLeft(), screen.bottomLeft())
    fill.setColorAt(0.0, QColor(18, 24, 32, 246))
    fill.setColorAt(1.0, QColor(6, 8, 12, 246))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(fill))
    p.drawPath(body)

    # Scanlines are the retro half of the look, and they are barely there:
    # at more than a few percent they stop being texture and start being noise.
    p.save()
    p.setClipPath(body)
    p.setPen(QPen(QColor(255, 255, 255, 7), 1))
    y = screen.top() + 1
    while y < screen.bottom():
        p.drawLine(QPointF(screen.left(), y), QPointF(screen.right(), y))
        y += SCANLINE_PX
    # A tint of the eye colour from below, as if the eyes lit the glass.
    tint = QLinearGradient(screen.topLeft(), screen.bottomLeft())
    tint.setColorAt(0.0, _rgba(color, 0.0))
    tint.setColorAt(1.0, _rgba(color, 0.07 * glow))
    p.fillRect(screen, QBrush(tint))
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(_rgba(_mix(color, (255, 255, 255), 0.3), 0.18 + 0.2 * glow), 1.2))
    p.drawPath(body)


def _draw_ring(p: QPainter, body: QPainterPath, screen: QRectF, frame: Frame) -> None:
    ring = frame.face.ring
    if ring < 0.02:
        return
    gradient = QConicalGradient(screen.center(), -frame.ring_phase * 360.0)
    color = frame.face.color
    gradient.setColorAt(0.0, _rgba(_mix(color, (255, 255, 255), 0.4), ring))
    gradient.setColorAt(0.18, _rgba(color, 0.55 * ring))
    gradient.setColorAt(0.42, _rgba(color, 0.0))
    gradient.setColorAt(0.96, _rgba(color, 0.0))
    gradient.setColorAt(1.0, _rgba(color, ring))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QBrush(gradient), 2.6))
    p.drawPath(body)


def _draw_eye(p: QPainter, eye: Eye, side: int, base_w: float, base_h: float,
              frame: Frame, glow: float) -> None:
    color = frame.face.color
    path = eye_path(eye, side, base_w, base_h)

    bounds = path.boundingRect()
    if bounds.height() < 0.5:
        return
    # The glow is a soft bloom behind the eye plus one tight halo: the eye
    # filled again a little larger and fainter. Both are fills, which are
    # several times cheaper than the wide stroked pens this used to use -
    # and a bloom does not care what shape the lids cut, where a large
    # scaled copy of a smile-eye's crescent drew a dark hill under it.
    p.setPen(Qt.PenStyle.NoPen)
    c = bounds.center()
    reach = max(bounds.width(), bounds.height()) * 0.95
    bloom = QRadialGradient(c, reach)
    bloom.setColorAt(0.0, _rgba(color, 0.22 * glow))
    bloom.setColorAt(0.55, _rgba(color, 0.07 * glow))
    bloom.setColorAt(1.0, _rgba(color, 0.0))
    p.setBrush(QBrush(bloom))
    p.drawEllipse(c, reach, reach)
    grow = 0.05 * base_h
    sx = (bounds.width() + 2 * grow) / max(bounds.width(), 1.0)
    sy = (bounds.height() + 2 * grow) / max(bounds.height(), 1.0)
    halo = QTransform().translate(c.x(), c.y()).scale(sx, sy).translate(-c.x(), -c.y()).map(path)
    p.setBrush(_rgba(color, 0.14 * glow))
    p.drawPath(halo)
    fill = QLinearGradient(bounds.topLeft(), bounds.bottomLeft())
    fill.setColorAt(0.0, _rgba(_mix(color, (255, 255, 255), 0.35), 1.0))
    fill.setColorAt(1.0, _rgba(color, 1.0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(fill))
    p.drawPath(path)

    w = base_w * eye.width
    h = base_h * eye.height * eye.open
    if h < base_h * 0.25:
        return  # mid-blink: a core on a slit reads as a glitch
    p.save()
    p.setClipPath(path)
    if eye.pupil > 0.01:
        # A soft core rather than a dot: a hard disc next to the glint reads
        # as two pips on a die, not as an eye looking somewhere.
        r = eye.pupil * min(w, h) * 0.75
        centre = QPointF(frame.face.gaze_x * w * PUPIL_TRAVEL, frame.face.gaze_y * h * PUPIL_TRAVEL)
        core = QRadialGradient(centre, r)
        core.setColorAt(0.0, _rgba(_mix(color, (255, 255, 255), 0.85), 0.95))
        core.setColorAt(0.45, _rgba(_mix(color, (255, 255, 255), 0.5), 0.45))
        core.setColorAt(1.0, _rgba(color, 0.0))
        p.setBrush(QBrush(core))
        p.drawEllipse(centre, r, r)
    # A fixed glint: the glass catching a light that does not move.
    g = min(w, h) * 0.075
    p.setBrush(QColor(255, 255, 255, 150))
    p.drawEllipse(QPointF(-w * 0.24, -h * 0.26), g, g)
    p.restore()


def paint(p: QPainter, rect: QRectF, frame: Frame) -> None:
    """Draw `frame` filling `rect`. The caller owns the painter and the clear."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    face = frame.face
    glow = face.glow * ((0.78 + 0.22 * frame.pulse) if face.pulse_hz > 0 else 1.0)

    screen = screen_rect(rect)
    dpr = p.device().devicePixelRatioF() if p.device() is not None else 1.0
    p.drawImage(rect.topLeft(), _screen_layer(rect.width(), rect.height(), dpr, face.color, glow))
    body = _body(screen)
    _draw_ring(p, body, screen, frame)

    sw, sh = screen.width(), screen.height()
    bob = face.bob * sh * math.sin(2 * math.pi * face.bob_hz * frame.t) if face.bob_hz > 0 else 0.0
    p.save()
    p.setClipPath(body)
    p.translate(screen.center().x() + face.gaze_x * GAZE_TRAVEL * sw,
                screen.center().y() - 0.02 * sh + face.gaze_y * GAZE_TRAVEL * sh + bob)
    p.rotate(face.roll)
    for eye, side in ((frame.left, -1), (frame.right, 1)):
        p.save()
        p.translate(side * (EYE_SPACING + eye.x) * sw, eye.y * sh)
        p.rotate(-side * eye.tilt)
        _draw_eye(p, eye, side, EYE_W * sw, EYE_H * sh, frame, glow)
        p.restore()
    p.restore()
