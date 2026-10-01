"""What E.V.'s face is doing, as numbers - with no drawing in it at all.

Everything here is plain Python: no Qt, no clock, no randomness it was not
handed. That is what lets the suite drive a blink frame by frame, and what
lets the same state machine sit behind any renderer - QPainter today, a
canvas in a browser preview, a test that only reads the numbers.

Three layers make a frame, and they are deliberately separate:

* **The mood** is the target: a set of eye parameters loaded from
  `expressions.json`. Changing mood never jumps; every scalar eases towards
  its new value with a time constant taken from the mood being entered, so a
  surprise snaps open and falling asleep takes most of a second.
* **The life** is procedural and runs on top of whatever the mood is: blinks
  on a randomised schedule, small saccades, a breathing bob, a pulse while
  E.V. is busy. A face that only moves when told to looks like a screensaver.
* **Presence** is the slow layer: the longer a session runs, the heavier the
  lids get. It is small on purpose - a tired face is charming once and
  annoying for the rest of the evening.

A mood is either a *base* (idle, thinking, speaking - it stays until replaced)
or *transient* (`hold_s` > 0: happy, confused, surprised - it plays out and
the base comes back). A transient is not cut short by a new base unless that
base sets `interrupt`, because "success" followed 50ms later by "idle" should
still read as a smile; lockdown is the one thing that must not wait for it.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

log = logging.getLogger("ev.face.expression")

LIBRARY_PATH = Path(__file__).with_name("expressions.json")

# The schema, as ranges. A value outside one is a typo in the JSON rather
# than a creative choice - a lid of 4.0 hides the eye entirely - so loading
# refuses it instead of drawing something nobody intended.
EYE_RANGES: dict[str, tuple[float, float]] = {
    "open": (0.0, 1.3),        # vertical openness; blinks multiply it
    "width": (0.4, 1.8),       # scale of the base eye width
    "height": (0.3, 1.8),      # scale of the base eye height
    "roundness": (0.0, 1.0),   # 0 square, 1 pill/circle
    "tilt": (-35.0, 35.0),     # degrees, >0 lifts the outer corner
    "lid_top": (0.0, 0.9),     # fraction of the eye hidden by the upper lid
    "lid_angle": (-40.0, 40.0),  # degrees, >0 drops the lid towards the nose
    "lid_bottom": (0.0, 0.9),  # lower lid rising as a curve: the smile-eye
    "pupil": (0.0, 1.0),       # size of the bright core; 0 = none
    "x": (-0.3, 0.3),          # outward offset, fraction of face width
    "y": (-0.3, 0.3),          # downward offset, fraction of face height
}
FACE_RANGES: dict[str, tuple[float, float]] = {
    "gaze_x": (-1.0, 1.0),
    "gaze_y": (-1.0, 1.0),
    "roll": (-30.0, 30.0),     # head tilt: the eye pair rotates together
    "glow": (0.0, 1.5),
    "bob": (0.0, 0.1),         # vertical bounce, fraction of face height
    "bob_hz": (0.0, 6.0),
    "ring": (0.0, 1.0),        # the processing ring round the screen
    "ring_speed": (0.0, 5.0),  # revolutions per second
    "pulse_hz": (0.0, 6.0),    # glow pulse; 0 = steady
}
_MOOD_KEYS = {
    "eyes", "left", "right", "face", "blink", "saccade",
    "track_cursor", "transition_ms", "hold_s", "interrupt",
}
_BLINK_KEYS = {"interval_s", "double_chance", "speed"}
_SACCADE_KEYS = {"interval_s", "range"}

BLINK_S = 0.17              # one blink at speed 1.0, close + hold + open
DOUBLE_BLINK_GAP_S = 0.11
SACCADE_TAU_S = 0.035       # eyes dart; they do not drift
CURSOR_WEIGHT = 0.7         # how far the cursor pulls the gaze
FATIGUE_START_S = 45 * 60
FATIGUE_FULL_S = 3 * 60 * 60


@dataclass(slots=True)
class Eye:
    open: float = 1.0
    width: float = 1.0
    height: float = 1.0
    roundness: float = 0.55
    tilt: float = 0.0
    lid_top: float = 0.0
    lid_angle: float = 0.0
    lid_bottom: float = 0.0
    pupil: float = 0.3
    x: float = 0.0
    y: float = 0.0


@dataclass(slots=True)
class Face:
    gaze_x: float = 0.0
    gaze_y: float = 0.0
    roll: float = 0.0
    color: tuple[float, float, float] = (92.0, 225.0, 255.0)
    glow: float = 0.65
    bob: float = 0.0
    bob_hz: float = 0.0
    ring: float = 0.0
    ring_speed: float = 0.0
    pulse_hz: float = 0.0


@dataclass(frozen=True, slots=True)
class Mood:
    name: str
    left: Eye
    right: Eye
    face: Face
    blink_interval: tuple[float, float]
    blink_double: float
    blink_speed: float
    saccade_interval: tuple[float, float]
    saccade_range: float
    track_cursor: bool
    transition_s: float
    hold_s: float
    interrupt: bool


@dataclass(slots=True)
class Frame:
    """Everything a renderer needs, already composed. Draw it; decide nothing."""

    mood: str
    left: Eye
    right: Eye
    face: Face
    t: float            # seconds since start, for bob and pulse phase
    ring_phase: float   # 0-1, where the processing ring's bright arc is
    pulse: float        # 0-1, current glow pulse


def parse_color(value: str) -> tuple[float, float, float]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"colour {value!r} is not #rrggbb")
    return tuple(float(int(text[i:i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def _check_range(where: str, key: str, value: Any, ranges: dict) -> float:
    if key not in ranges:
        raise ValueError(f"{where}: unknown key {key!r}")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{where}.{key}: expected a number, got {value!r}")
    lo, hi = ranges[key]
    if not lo <= value <= hi:
        raise ValueError(f"{where}.{key}={value} is outside {lo}..{hi}")
    return float(value)


def _apply_eye(eye: Eye, where: str, overrides: dict) -> Eye:
    values = {k: _check_range(where, k, v, EYE_RANGES) for k, v in overrides.items()}
    return replace(eye, **values)


def _apply_face(face: Face, where: str, overrides: dict) -> Face:
    values: dict[str, Any] = {}
    for key, value in overrides.items():
        if key == "color":
            values["color"] = parse_color(value)
        else:
            values[key] = _check_range(where, key, value, FACE_RANGES)
    return replace(face, **values)


def _interval(where: str, value: Any) -> tuple[float, float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(isinstance(v, (int, float)) for v in value)
            or not 0 < value[0] <= value[1]):
        raise ValueError(f"{where}: interval_s must be [min, max] seconds, min > 0")
    return float(value[0]), float(value[1])


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def build_mood(name: str, spec: dict, defaults: dict) -> Mood:
    where = f"moods.{name}"
    unknown = {k for k in spec if not k.startswith("_")} - _MOOD_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}")
    merged = _merge(defaults, spec)

    shared = _apply_eye(Eye(), f"{where}.defaults.eye", defaults.get("eye", {}))
    shared = _apply_eye(shared, f"{where}.eyes", spec.get("eyes", {}))
    left = _apply_eye(shared, f"{where}.left", spec.get("left", {}))
    right = _apply_eye(shared, f"{where}.right", spec.get("right", {}))
    face = _apply_face(Face(), f"{where}.face", merged.get("face", {}))

    blink = merged.get("blink", {})
    saccade = merged.get("saccade", {})
    for part, keys, label in ((blink, _BLINK_KEYS, "blink"), (saccade, _SACCADE_KEYS, "saccade")):
        extra = set(part) - keys
        if extra:
            raise ValueError(f"{where}.{label}: unknown key(s) {sorted(extra)}")
    double = float(blink.get("double_chance", 0.0))
    speed = float(blink.get("speed", 1.0))
    if not 0.0 <= double <= 1.0 or not 0.1 <= speed <= 4.0:
        raise ValueError(f"{where}.blink: double_chance 0..1, speed 0.1..4")
    transition_ms = float(merged.get("transition_ms", 160))
    hold_s = float(merged.get("hold_s", 0))
    if transition_ms < 0 or hold_s < 0:
        raise ValueError(f"{where}: transition_ms and hold_s must not be negative")

    return Mood(
        name=name,
        left=left,
        right=right,
        face=face,
        blink_interval=_interval(f"{where}.blink", blink.get("interval_s", [3, 6])),
        blink_double=double,
        blink_speed=speed,
        saccade_interval=_interval(f"{where}.saccade", saccade.get("interval_s", [1, 3])),
        saccade_range=float(saccade.get("range", 0.0)),
        track_cursor=bool(merged.get("track_cursor", False)),
        transition_s=transition_ms / 1000.0,
        hold_s=hold_s,
        interrupt=bool(merged.get("interrupt", False)),
    )


@dataclass
class Library:
    moods: dict[str, Mood]
    events: dict[str, str] = field(default_factory=dict)

    def mood(self, name: str) -> Mood:
        found = self.moods.get(name.strip().lower())
        if found is None:
            log.debug("unknown mood %r, using neutral", name)
            return self.moods["neutral"]
        return found


def load_library(path: Path | str | None = None) -> Library:
    """Load and validate the mood file. Raises ValueError naming the bad key."""
    raw = json.loads(Path(path or LIBRARY_PATH).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    moods = {
        name.lower(): build_mood(name.lower(), spec or {}, defaults)
        for name, spec in raw.get("moods", {}).items()
    }
    if "neutral" not in moods:
        moods["neutral"] = build_mood("neutral", {}, defaults)
    events = {k.lower(): v.lower() for k, v in raw.get("events", {}).items()}
    missing = sorted(v for v in events.values() if v not in moods)
    if missing:
        raise ValueError(f"events point at moods that do not exist: {missing}")
    return Library(moods=moods, events=events)


def still_frame(mood: Mood) -> Frame:
    """A mood at rest - eyes open, no saccade - for previews and contact sheets."""
    return Frame(mood=mood.name, left=replace(mood.left), right=replace(mood.right),
                 face=replace(mood.face), t=0.0, ring_phase=0.12, pulse=0.5)


def blink_closure(elapsed: float, duration: float) -> float:
    """How shut the lids are, 0-1, `elapsed` seconds into a blink.

    Lids fall fast and rise slowly - 35% closing, 15% shut, 50% opening -
    which is what makes a blink read as a blink rather than a flicker.
    """
    if elapsed <= 0 or elapsed >= duration:
        return 0.0
    p = elapsed / duration
    if p < 0.35:
        return _ease_in(p / 0.35)
    if p < 0.5:
        return 1.0
    return 1.0 - _ease_out((p - 0.5) / 0.5)


def fatigue(session_s: float) -> float:
    """0 for the first 45 minutes, rising to 1 at three hours."""
    span = FATIGUE_FULL_S - FATIGUE_START_S
    return min(1.0, max(0.0, (session_s - FATIGUE_START_S) / span))


def _ease_in(p: float) -> float:
    return p * p


def _ease_out(p: float) -> float:
    return 1.0 - (1.0 - p) ** 3


def _approach(current: float, target: float, k: float) -> float:
    return current + (target - current) * k


def _blend_eye(cur: Eye, target: Eye, k: float) -> Eye:
    return Eye(**{f.name: _approach(getattr(cur, f.name), getattr(target, f.name), k) for f in fields(Eye)})


def _blend_face(cur: Face, target: Face, k: float) -> Face:
    values: dict[str, Any] = {}
    for f in fields(Face):
        a, b = getattr(cur, f.name), getattr(target, f.name)
        if f.name == "color":
            values["color"] = tuple(_approach(x, y, k) for x, y in zip(a, b))
        else:
            values[f.name] = _approach(a, b, k)
    return Face(**values)


class Expression:
    """The face's state machine. Feed it events and time; read frames out."""

    def __init__(self, library: Library | None = None, mood: str = "idle",
                 rng: random.Random | None = None) -> None:
        self.library = library or load_library()
        self._rng = rng or random.Random()
        self.base = self.library.mood(mood)
        self.transient: Mood | None = None
        self._transient_until = 0.0
        self.now = 0.0
        self.session_s = 0.0

        self._left = replace(self.base.left)
        self._right = replace(self.base.right)
        self._face = replace(self.base.face)

        self._blink_start: float | None = None
        self._blink_duration = BLINK_S
        self._next_blink = self._schedule(self.base.blink_interval)
        self._saccade_target = (0.0, 0.0)
        self._saccade = (0.0, 0.0)
        self._next_saccade = self._schedule(self.base.saccade_interval)
        self._cursor = (0.0, 0.0)
        self._ring_phase = 0.0
        self._changed_at = -10.0

    # -- input ----------------------------------------------------------

    @property
    def active(self) -> Mood:
        return self.transient or self.base

    @property
    def mood(self) -> str:
        return self.active.name

    def set_mood(self, name: str) -> None:
        mood = self.library.mood(name)
        before = self.active
        if mood.hold_s > 0:
            self.transient = mood
            self._transient_until = self.now + mood.hold_s
        else:
            self.base = mood
            if mood.interrupt:
                self.transient = None
        if self.active is not before:
            self._changed_at = self.now

    def event(self, name: str) -> bool:
        """Map a core event ('thinking', 'success', ...) to a mood. False if unknown."""
        target = self.library.events.get(name.strip().lower())
        if target is None:
            return False
        self.set_mood(target)
        return True

    def look_at(self, x: float, y: float) -> None:
        """Where the cursor is, -1..1 in each axis relative to the face."""
        self._cursor = (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))

    def blink(self) -> None:
        if self._blink_start is None:
            self._start_blink()

    @property
    def settling(self) -> bool:
        """Whether something fast is moving: a blink, a dart, a change of mood.

        The window renders at full rate only while this is true. Between those
        moments the face is a slow bob and a turning ring, which look the same
        at half the frames - and on XWayland every frame is a full-window copy.
        """
        if self._blink_start is not None:
            return True
        if self.now - self._changed_at < max(0.3, self.active.transition_s * 1.5):
            return True
        dx = self._saccade_target[0] - self._saccade[0]
        dy = self._saccade_target[1] - self._saccade[1]
        return dx * dx + dy * dy > 1e-4

    # -- time -------------------------------------------------------------

    def tick(self, dt: float) -> Frame:
        dt = max(0.0, min(dt, 0.25))  # a stalled loop must not teleport the eyes
        self.now += dt
        self.session_s += dt

        if self.transient and self.now >= self._transient_until:
            self.transient = None
            self._changed_at = self.now
        mood = self.active

        tau = max(mood.transition_s / 3.0, 1e-3)
        k = 1.0 - math.exp(-dt / tau)
        self._left = _blend_eye(self._left, mood.left, k)
        self._right = _blend_eye(self._right, mood.right, k)
        self._face = _blend_face(self._face, mood.face, k)

        closure = self._update_blink(mood)
        self._update_saccade(mood, dt)
        self._ring_phase = (self._ring_phase + self._face.ring_speed * dt) % 1.0

        tired = fatigue(self.session_s)
        droop = 1.0 - 0.12 * tired
        left = replace(self._left, open=self._left.open * (1.0 - closure) * droop,
                       lid_top=min(0.9, self._left.lid_top + 0.08 * tired))
        right = replace(self._right, open=self._right.open * (1.0 - closure) * droop,
                        lid_top=min(0.9, self._right.lid_top + 0.08 * tired))

        gx, gy = self._face.gaze_x + self._saccade[0], self._face.gaze_y + self._saccade[1]
        if mood.track_cursor:
            gx += self._cursor[0] * CURSOR_WEIGHT
            gy += self._cursor[1] * CURSOR_WEIGHT
        face = replace(self._face, gaze_x=max(-1.0, min(1.0, gx)), gaze_y=max(-1.0, min(1.0, gy)))

        pulse = 0.0
        if face.pulse_hz > 0:
            pulse = 0.5 + 0.5 * math.sin(2 * math.pi * face.pulse_hz * self.now)
        return Frame(mood=mood.name, left=left, right=right, face=face,
                     t=self.now, ring_phase=self._ring_phase, pulse=pulse)

    # -- procedural life --------------------------------------------------

    def _schedule(self, interval: tuple[float, float], scale: float = 1.0) -> float:
        return self.now + self._rng.uniform(*interval) * scale

    def _start_blink(self) -> None:
        self._blink_start = self.now
        self._blink_duration = BLINK_S / self.active.blink_speed

    def _update_blink(self, mood: Mood) -> float:
        if self._blink_start is None and self.now >= self._next_blink:
            self._start_blink()
        if self._blink_start is None:
            return 0.0
        elapsed = self.now - self._blink_start
        if elapsed < self._blink_duration:
            return blink_closure(elapsed, self._blink_duration)
        self._blink_start = None
        if self._rng.random() < mood.blink_double:
            self._next_blink = self.now + DOUBLE_BLINK_GAP_S
        else:
            slower = 1.0 + 0.5 * fatigue(self.session_s)
            self._next_blink = self._schedule(mood.blink_interval, slower)
        return 0.0

    def _update_saccade(self, mood: Mood, dt: float) -> None:
        if self.now >= self._next_saccade:
            r = mood.saccade_range
            # Most glances are small and a few are large, which is how eyes
            # actually move; a uniform spread looks like a random walk.
            self._saccade_target = (self._rng.gauss(0, r / 2), self._rng.gauss(0, r / 3))
            self._next_saccade = self._schedule(mood.saccade_interval)
        if mood.saccade_range == 0:
            self._saccade_target = (0.0, 0.0)
        k = 1.0 - math.exp(-dt / SACCADE_TAU_S)
        self._saccade = (_approach(self._saccade[0], self._saccade_target[0], k),
                         _approach(self._saccade[1], self._saccade_target[1], k))


def apply_command(expression: Expression, line: str) -> bool:
    """Apply one line of the face's stdin protocol. False means close the face.

    One JSON object per line: `{"event": "thinking"}`, `{"mood": "happy"}`,
    `{"look": [x, y]}` or `{"quit": true}`. A bare word is tried as an event
    and then as a mood, which is what makes the pipe usable by hand.
    """
    text = line.strip()
    if not text:
        return True
    try:
        message = json.loads(text) if text.startswith("{") else {"word": text}
    except json.JSONDecodeError:
        log.debug("ignoring malformed line %r", text)
        return True
    if not isinstance(message, dict):
        return True
    if message.get("quit"):
        return False
    if "event" in message:
        expression.event(str(message["event"]))
    if "mood" in message:
        expression.set_mood(str(message["mood"]))
    word = str(message.get("word", "")).strip().lower()
    # A bare word that is neither an event nor a mood is ignored, not
    # mapped to neutral: a stray line on the pipe must not wipe the face.
    if word and not expression.event(word) and word in expression.library.moods:
        expression.set_mood(word)
    look = message.get("look")
    if isinstance(look, (list, tuple)) and len(look) == 2:
        try:
            expression.look_at(float(look[0]), float(look[1]))
        except (TypeError, ValueError):
            pass
    return True
