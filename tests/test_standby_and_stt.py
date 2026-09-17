"""Standby recovery, app resolution, and the speech-recognition gate.

Three regressions live here, all reported from real use:

* **Standby was a black hole.** `match_intent` is exact, so "hey, wake up"
  fell through it and hit a bare `return`. The user saw their own words echoed
  and got nothing back, with no way to tell a sleeping assistant from a broken
  one.
* **An unknown app cost ten seconds and an error window.** The old fallback
  shelled out to `cmd /c start`, which pops a *modal dialog* for a name
  Windows cannot find and blocks until it is dismissed.
* **A garbled transcript became a confidently wrong action.** Whisper reports
  how sure it was; E.V. was asking for plain `json` and throwing it away.

Everything here is offline. No microphone, no network, no program launched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from ev.session import Intent, Mode, is_resume_phrase, match_intent  # noqa: E402
from ev.stt import Transcript, _score  # noqa: E402


# -- standby recovery --------------------------------------------------------
@pytest.mark.parametrize(
    "said",
    [
        "wake up",
        "Wake up!",
        "hey wake up",
        "okay wake up",
        "EV, wake up",
        "you awake?",
        "you there?",
        "come back",
        "resume",
        "unmute",
        "start listening",
        "rise and shine",
        "break's over",
    ],
)
def test_every_reasonable_way_of_saying_wake_up_gets_through(said):
    assert is_resume_phrase(said) is True


@pytest.mark.parametrize(
    "said",
    [
        "stop",
        "stop the server",
        "delete the build folder",
        "open chrome",
        "what's the weather",
        "",
        "   ",
    ],
)
def test_ordinary_speech_is_not_mistaken_for_a_wake_up(said):
    assert is_resume_phrase(said) is False


def test_a_long_sentence_mentioning_waking_is_ignored():
    """Someone talking in the room is not addressing a sleeping assistant."""
    assert is_resume_phrase("so anyway I told him to wake up early tomorrow") is False


def test_the_exact_matcher_is_still_exact():
    """The loose pass is additive; `match_intent` must not have loosened."""
    assert match_intent("stop the server", Mode.ENGAGED) is None
    assert match_intent("stop", Mode.ENGAGED) is Intent.CANCEL
    # Standby still ignores everything but coming back and shutting down.
    assert match_intent("open chrome", Mode.STANDBY) is None
    assert match_intent("wake up", Mode.STANDBY) is Intent.RESUME
    assert match_intent("goodbye", Mode.STANDBY) is Intent.SHUTDOWN


def test_resume_matching_costs_nothing_when_already_awake():
    """The loose pass only runs in standby, so this stays a check-in."""
    assert match_intent("you there", Mode.ENGAGED) is Intent.STATUS


# -- the speech-recognition gate ---------------------------------------------
def test_clear_speech_passes_straight_through():
    clear = Transcript("open chrome", avg_logprob=-0.25, no_speech=0.02, compression=1.4)
    assert clear.rejected is False
    assert clear.uncertain is False
    assert clear.scored is True


def test_a_mumble_is_flagged_rather_than_acted_on():
    mumble = Transcript("elite the bill folder", avg_logprob=-1.4, no_speech=0.2)
    assert mumble.rejected is True


def test_audio_that_was_not_speech_is_rejected():
    """Measured from a real Groq response to a 180Hz tone: no_speech 0.79."""
    tone = Transcript("you", avg_logprob=-0.67, no_speech=0.79)
    assert tone.rejected is True


def test_a_repetition_loop_is_caught():
    """Whisper's classic failure: one phrase repeated until the buffer ends."""
    looped = Transcript("yes yes yes yes yes", avg_logprob=-0.3, compression=3.1)
    assert looped.rejected is True


def test_a_borderline_transcript_is_used_but_marked():
    borderline = Transcript("open chrome", avg_logprob=-0.75, no_speech=0.1)
    assert borderline.rejected is False
    # Acted on, but the model is told the words may be wrong.
    assert borderline.uncertain is True


def test_backends_that_report_no_confidence_are_left_alone():
    """Google and whisper.cpp score nothing; the gate must not reject them."""
    plain = Transcript("open chrome")
    assert plain.scored is False
    assert plain.rejected is False
    assert plain.uncertain is False


def test_the_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "STT_CONFIDENCE_GATE", False)
    awful = Transcript("nonsense", avg_logprob=-3.0, no_speech=0.99, compression=9.0)
    assert awful.rejected is False
    assert awful.uncertain is False


def test_a_transcript_still_behaves_exactly_like_a_string():
    """Every existing call site treats this as text, and must keep working."""
    transcript = Transcript("Open Chrome", avg_logprob=-0.2)
    assert transcript.lower() == "open chrome"
    assert f"{transcript}" == "Open Chrome"
    assert transcript == "Open Chrome"
    assert bool(Transcript("")) is False
    assert match_intent(Transcript("stop"), Mode.ENGAGED) is Intent.CANCEL


# -- confidence extraction ---------------------------------------------------
def test_scores_are_weighted_by_segment_length():
    """A long confident sentence is not dragged down by a short mumble."""
    payload = {
        "segments": [
            {"start": 0.0, "end": 9.0, "avg_logprob": -0.2, "no_speech_prob": 0.01,
             "compression_ratio": 1.5},
            {"start": 9.0, "end": 10.0, "avg_logprob": -1.2, "no_speech_prob": 0.30,
             "compression_ratio": 1.6},
        ]
    }
    avg, no_speech, compression = _score(payload)
    assert -0.35 < avg < -0.25  # nearer the long segment than the short one
    # The worst segment is what matters for these two, not the average.
    assert no_speech == 0.30
    assert compression == 1.6


def test_a_response_with_no_segments_scores_as_unknown():
    assert _score({"text": "hello"}) == (0.0, 0.0, 0.0)


def test_malformed_segments_are_skipped_not_fatal():
    payload = {"segments": [{"start": "nope", "end": None}, "garbage", 7]}
    assert _score(payload) == (0.0, 0.0, 0.0)


# -- the decoding prompt -----------------------------------------------------
@pytest.fixture
def transcriber():
    from ev.stt import Transcriber

    return Transcriber()


def test_machine_specific_names_reach_the_prompt(transcriber, monkeypatch):
    monkeypatch.setattr(config, "STT_DYNAMIC_PROMPT", True)
    transcriber.set_hints(["Parsec", "ShareX", "Rainmeter"])
    prompt = transcriber._prompt()
    assert config.STT_VOCABULARY in prompt
    assert "Parsec" in prompt and "ShareX" in prompt


def test_the_previous_utterance_goes_last(transcriber, monkeypatch):
    """Whisper weights the end of the prompt most - it is the nearest text."""
    monkeypatch.setattr(config, "STT_DYNAMIC_PROMPT", True)
    transcriber.set_hints(["Parsec"])
    transcriber.note_transcript("open OBS and start the stream")
    assert transcriber._prompt().endswith("open OBS and start the stream")


def test_the_prompt_never_exceeds_whispers_window(transcriber, monkeypatch):
    """Whisper silently drops the front of an over-long prompt."""
    monkeypatch.setattr(config, "STT_DYNAMIC_PROMPT", True)
    transcriber.set_hints([f"Application Number {n}" for n in range(500)])
    transcriber.note_transcript("x" * 200)
    assert len(transcriber._prompt()) <= config.STT_PROMPT_MAX_CHARS


def test_hints_are_deduplicated_case_insensitively(transcriber):
    transcriber.set_hints(["Chrome", "chrome", "CHROME", "Edge"])
    assert transcriber._hints == ["Chrome", "Edge"]


def test_dynamic_hints_can_be_switched_off(transcriber, monkeypatch):
    monkeypatch.setattr(config, "STT_DYNAMIC_PROMPT", False)
    transcriber.set_hints(["Parsec"])
    transcriber.note_transcript("something")
    assert transcriber._prompt() == config.STT_VOCABULARY


# -- app resolution ----------------------------------------------------------
def test_spoken_filler_is_stripped_from_app_names():
    from tools.app_launcher import _normalise_name

    assert _normalise_name("the Spotify app") == "spotify"
    assert _normalise_name("  Chrome.  ") == "chrome"
    assert _normalise_name("my Discord application") == "discord"


def test_a_missing_app_fails_fast_and_says_so():
    """This used to block for ten seconds and leave an error dialog on screen."""
    import time

    from tools import dispatch

    started = time.monotonic()
    result = dispatch("open_app", {"app": "zzz-not-a-real-program-9999"})
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert "find" in result.speech.lower()
    # The old `cmd /c start` fallback was capped at a 10s timeout; anything
    # near that means the blocking shell call is back.
    assert elapsed < 3.0


def test_nothing_shells_out_to_start_any_more():
    """A structural check: `start` is what drew the modal dialog."""
    source = (Path(__file__).resolve().parent.parent / "tools" / "app_launcher.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert '"start"' not in code
    assert "subprocess.run" not in code


def test_the_index_prefers_the_shortest_sensible_match():
    from tools.app_launcher import _AppIndex

    apps = {
        "code": "code.lnk",
        "code - insiders": "insiders.lnk",
        "google chrome": "chrome.lnk",
    }
    assert _AppIndex._match(apps, "code") == ("code", "code.lnk")
    # A word contained in a longer name still resolves.
    assert _AppIndex._match(apps, "chrome") == ("google chrome", "chrome.lnk")
    assert _AppIndex._match(apps, "zzz-nothing") is None
    assert _AppIndex._match({}, "chrome") is None


def test_installer_and_documentation_shortcuts_are_not_indexed(tmp_path, monkeypatch):
    """"Uninstall Spotify" is a shortcut, but nobody means it by "Spotify"."""
    import tools.app_launcher as launcher

    programs = tmp_path / "Programs"
    (programs / "Vendor").mkdir(parents=True)
    for name in ("Spotify.lnk", "Uninstall Spotify.lnk", "Spotify Readme.lnk"):
        (programs / "Vendor" / name).write_bytes(b"")

    monkeypatch.setattr(launcher, "_start_menu_dirs", lambda: [programs])
    found = launcher._scan_start_menu()

    assert "spotify" in found
    assert not any("uninstall" in name for name in found)
    assert not any("readme" in name for name in found)


def test_the_index_is_cached_and_reused(tmp_path, monkeypatch):
    import tools.app_launcher as launcher

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_INDEX_ENABLED", True)
    monkeypatch.setattr(launcher, "IS_WINDOWS", True)

    scans = []

    def _fake_scan():
        scans.append(1)
        return {"spotify": "spotify.lnk"}

    monkeypatch.setattr(launcher, "_scan_start_menu", _fake_scan)

    first = launcher._AppIndex()
    assert first.apps() == {"spotify": "spotify.lnk"}
    assert (tmp_path / "apps.json").exists()

    # A fresh index in the same process reads the cache instead of rescanning.
    second = launcher._AppIndex()
    assert second.apps() == {"spotify": "spotify.lnk"}
    assert len(scans) == 1


def test_a_miss_triggers_one_rescan_for_newly_installed_programs(tmp_path, monkeypatch):
    import tools.app_launcher as launcher

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_INDEX_ENABLED", True)
    monkeypatch.setattr(launcher, "IS_WINDOWS", True)
    monkeypatch.setattr(config, "APP_INDEX_RESCAN_S", 0.0)
    monkeypatch.setattr(launcher, "_scan_start_menu", lambda: {"spotify": "a.lnk"})

    index = launcher._AppIndex()
    index.apps()
    # Something installed since the cache was written.
    monkeypatch.setattr(
        launcher, "_scan_start_menu", lambda: {"spotify": "a.lnk", "parsec": "b.lnk"}
    )
    assert index.find("parsec") == ("parsec", "b.lnk")


def test_a_name_that_does_not_exist_does_not_rescan_every_time(tmp_path, monkeypatch):
    """Otherwise one misheard word walks the Start Menu on every attempt."""
    import tools.app_launcher as launcher

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_INDEX_ENABLED", True)
    monkeypatch.setattr(config, "APP_INDEX_RESCAN_S", 600.0)
    monkeypatch.setattr(launcher, "IS_WINDOWS", True)

    scans = []
    monkeypatch.setattr(
        launcher, "_scan_start_menu", lambda: (scans.append(1), {"spotify": "a.lnk"})[1]
    )

    index = launcher._AppIndex()
    for _ in range(5):
        assert index.find("nothing-like-this") is None
    assert len(scans) == 1


def test_the_index_is_empty_when_switched_off(tmp_path, monkeypatch):
    import tools.app_launcher as launcher

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_INDEX_ENABLED", False)
    assert launcher._AppIndex().apps() == {}
