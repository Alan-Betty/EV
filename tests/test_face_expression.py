"""The face's state machine: numbers in, numbers out, no Qt.

The renderer is checked at the bottom and skipped when PySide6 is missing,
which is the usual case in the project venv - the face is optional, and the
state machine is the part with behaviour worth pinning.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

from ev.face import expression as ex  # noqa: E402
from ev.face.expression import Expression, apply_command, load_library  # noqa: E402

DT = 1 / 60


def _settle(face: Expression, seconds: float) -> ex.Frame:
    frame = face.tick(0)
    for _ in range(int(seconds / DT)):
        frame = face.tick(DT)
    return frame


def _write(tmp_path, data) -> Path:
    path = tmp_path / "moods.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- the shipped library ------------------------------------------------------

def test_shipped_library_loads_and_every_event_has_a_mood():
    library = load_library()
    for required in ("neutral", "idle", "listening", "thinking", "speaking",
                     "happy", "skeptical", "confused", "sleepy", "alert"):
        assert required in library.moods
    assert all(target in library.moods for target in library.events.values())


def test_core_events_are_all_mapped():
    """Every state the core loop can be in has a face, or the face lies about it."""
    events = load_library().events
    for name in ("idle", "listening", "thinking", "speaking", "tool", "confirm",
                 "success", "error", "standby", "lockdown"):
        assert name in events


# -- validation ---------------------------------------------------------------

def test_unknown_eye_key_is_refused_with_its_location(tmp_path):
    path = _write(tmp_path, {"moods": {"odd": {"eyes": {"pupl": 0.3}}}})
    with pytest.raises(ValueError, match=r"moods\.odd\.eyes.*pupl"):
        load_library(path)


def test_out_of_range_value_is_refused(tmp_path):
    path = _write(tmp_path, {"moods": {"odd": {"left": {"lid_top": 4.0}}}})
    with pytest.raises(ValueError, match="lid_top"):
        load_library(path)


def test_event_pointing_at_missing_mood_is_refused(tmp_path):
    path = _write(tmp_path, {"moods": {}, "events": {"thinking": "pondering"}})
    with pytest.raises(ValueError, match="pondering"):
        load_library(path)


def test_left_and_right_override_the_shared_eye(tmp_path):
    path = _write(tmp_path, {"moods": {"wink": {"eyes": {"height": 1.2}, "left": {"open": 0.1}}}})
    mood = load_library(path).moods["wink"]
    assert mood.left.open == 0.1 and mood.right.open == 1.0
    assert mood.left.height == mood.right.height == 1.2


def test_unknown_mood_falls_back_to_neutral_without_raising():
    face = Expression(mood="no-such-mood", rng=random.Random(1))
    assert face.mood == "neutral"


# -- transitions ----------------------------------------------------------------

def test_mood_change_eases_rather_than_jumps():
    face = Expression(mood="neutral", rng=random.Random(1))
    _settle(face, 0.5)
    face.set_mood("surprised")
    first = face.tick(DT)
    target = face.library.moods["surprised"].left.height
    assert 1.0 < first.left.height < target  # moving, not there yet
    settled = _settle(face, 1.0)
    assert settled.left.height == pytest.approx(target, abs=0.01)


def test_transient_mood_plays_out_then_base_returns():
    face = Expression(mood="idle", rng=random.Random(1))
    face.event("success")
    assert face.mood == "happy"
    face.event("thinking")  # a new base does not cut the smile short
    assert face.mood == "happy"
    _settle(face, face.library.moods["happy"].hold_s + 0.1)
    assert face.mood == "thinking"


def test_interrupting_mood_cuts_a_transient_short(tmp_path):
    path = _write(tmp_path, {"moods": {
        "happy": {"hold_s": 5},
        "alert": {"interrupt": True},
    }})
    face = Expression(load_library(path), mood="neutral", rng=random.Random(1))
    face.set_mood("happy")
    face.set_mood("alert")
    assert face.mood == "alert"


def test_unknown_event_is_ignored():
    face = Expression(mood="idle", rng=random.Random(1))
    assert face.event("not-an-event") is False
    assert face.mood == "idle"


# -- procedural life ------------------------------------------------------------

def test_blink_closes_fully_and_reopens():
    d = ex.BLINK_S
    samples = [ex.blink_closure(t * d / 100, d) for t in range(101)]
    assert samples[0] == 0 and samples[-1] == 0
    assert max(samples) == 1.0
    # Lids fall faster than they rise.
    closing = next(i for i, v in enumerate(samples) if v == 1.0)
    reopening = len(samples) - 1 - max(i for i, v in enumerate(samples) if v == 1.0)
    assert closing < reopening


def test_blinks_happen_on_their_own():
    face = Expression(mood="neutral", rng=random.Random(3))
    face.tick(0)
    opens = [face.tick(DT).left.open for _ in range(int(10 / DT))]
    assert min(opens) < 0.2  # at least one blink in ten seconds
    assert max(opens) > 0.95


def test_cursor_only_moves_eyes_in_tracking_moods():
    tracking = Expression(mood="idle", rng=random.Random(1))
    tracking.look_at(1.0, 0.0)
    still = Expression(mood="surprised", rng=random.Random(1))
    still.look_at(1.0, 0.0)
    assert _settle(tracking, 0.5).face.gaze_x > 0.4
    assert abs(_settle(still, 0.5).face.gaze_x) < 0.05


def test_ring_turns_only_while_processing():
    face = Expression(mood="thinking", rng=random.Random(1))
    a = _settle(face, 0.2).ring_phase
    b = _settle(face, 0.2).ring_phase
    assert a != b
    idle = Expression(mood="idle", rng=random.Random(1))
    assert _settle(idle, 1.0).ring_phase == 0.0


def test_a_stalled_loop_does_not_teleport_the_eyes():
    face = Expression(mood="neutral", rng=random.Random(1))
    face.tick(0)
    face.set_mood("surprised")
    frame = face.tick(30.0)  # the machine slept
    assert frame.left.height < face.library.moods["surprised"].left.height


def test_long_sessions_droop_the_lids_a_little():
    assert ex.fatigue(10 * 60) == 0
    assert 0 < ex.fatigue(90 * 60) < 1
    assert ex.fatigue(5 * 3600) == 1
    face = Expression(mood="neutral", rng=random.Random(1))
    fresh = face.tick(0).left.open
    face.session_s = 5 * 3600
    tired = face.tick(0).left.open
    assert 0.8 < tired / fresh < 1.0


# -- the stdin protocol ---------------------------------------------------------

def test_protocol_accepts_events_moods_words_and_quit():
    face = Expression(mood="idle", rng=random.Random(1))
    assert apply_command(face, '{"event": "thinking"}') and face.mood == "thinking"
    assert apply_command(face, '{"mood": "speaking"}') and face.mood == "speaking"
    assert apply_command(face, "lockdown") and face.mood == "alert"   # bare event
    assert apply_command(face, "sleepy") and face.mood == "sleepy"    # bare mood
    assert apply_command(face, '{"quit": true}') is False


def test_protocol_shrugs_off_garbage():
    face = Expression(mood="idle", rng=random.Random(1))
    for line in ("", "{not json", "[1, 2]", '{"look": ["a", "b"]}', '{"look": 5}'):
        assert apply_command(face, line) is True
    assert face.mood == "idle"


# -- rendering (needs PySide6) ----------------------------------------------------

def test_every_mood_renders_with_a_transparent_outside():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from ev.face import render

    # A QApplication rather than a QGuiApplication: Qt allows one per
    # process, and the window tests in test_face_integration need widgets.
    app = QGuiApplication.instance() or QApplication([])  # noqa: F841
    library = load_library()
    for name, mood in library.moods.items():
        image = QImage(240, 192, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        render.paint(painter, QRectF(0, 0, 240, 192), ex.still_frame(mood))
        painter.end()
        assert image.pixelColor(0, 0).alpha() == 0, f"{name}: corner not transparent"
        centre = image.pixelColor(120, 96)
        assert centre.alpha() > 200, f"{name}: screen not drawn"


def test_settling_is_true_only_while_something_fast_moves(tmp_path):
    """The window renders at full rate only while this is true, so it must fall quiet."""
    path = _write(tmp_path, {
        "defaults": {"blink": {"interval_s": [50, 60]}, "saccade": {"interval_s": [50, 60], "range": 0}},
        "moods": {"calm": {}, "wide": {"eyes": {"height": 1.4}}},
    })
    face = Expression(load_library(path), mood="calm", rng=random.Random(1))
    _settle(face, 1.0)
    assert not face.settling
    face.set_mood("wide")
    assert face.settling
    _settle(face, 1.0)
    assert not face.settling
    face.blink()
    face.tick(DT)
    assert face.settling
