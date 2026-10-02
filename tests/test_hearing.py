"""Learning how this user sounds, and getting out of standby by name.

Two complaints from real use, both about E.V. not adapting to the person:

* **Speech recognition never got better.** Whisper is an HTTP call, so
  nothing can be retrained - but the decoding prompt and the confidence gate
  can both be learned from the user's own addressed speech. `ev.hearing`
  does that, and these tests pin what it may and may not learn from.
* **Standby needed a password.** Saying "E.V." - or anything Whisper makes
  of it - to a sleeping assistant did nothing unless "wake up" came with it.

Offline; every profile lives in `tmp_path`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
import ev_core  # noqa: E402
from ev import hearing, wake  # noqa: E402
from ev.hearing import HearingProfile  # noqa: E402
from ev.session import Intent, Session  # noqa: E402
from ev.stt import Transcriber, Transcript  # noqa: E402


@pytest.fixture
def learning(tmp_path, monkeypatch):
    """Learning on, against a profile of this test's own."""
    monkeypatch.setattr(config, "STT_LEARN", True)
    monkeypatch.setattr(config, "HEARING_FILE", tmp_path / "hearing.json")
    monkeypatch.setattr(config, "STT_DYNAMIC_PROMPT", True)
    monkeypatch.setattr(hearing, "_profile", None)
    return tmp_path / "hearing.json"


def _clear(text: str) -> Transcript:
    return Transcript(text, avg_logprob=-0.2, no_speech=0.01, compression=1.3)


# -- vocabulary ----------------------------------------------------------------


def test_recurring_words_are_learned_and_filler_is_not(learning):
    profile = HearingProfile()
    for _ in range(3):
        profile.learn("open Kubernetes dashboard please", rejected=False,
                      uncertain=False, avg_logprob=-0.2)
    vocab = profile.vocabulary()
    assert "Kubernetes" in vocab and "dashboard" in vocab
    assert "open" not in [w.lower() for w in vocab]
    assert "please" not in [w.lower() for w in vocab]


def test_one_mention_is_chatter_not_vocabulary(learning):
    profile = HearingProfile()
    profile.learn("tell Priyanka hello", rejected=False, uncertain=False, avg_logprob=-0.2)
    assert profile.vocabulary() == []


def test_a_clear_repeat_after_a_miss_teaches_the_corrected_word(learning):
    """The best evidence there is: the word that changed is the one misheard."""
    profile = HearingProfile()
    profile.learn("run obese", rejected=True, uncertain=False, avg_logprob=-1.3, now=100.0)
    profile.learn("run OBS", rejected=False, uncertain=False, avg_logprob=-0.2, now=104.0)
    assert profile.vocabulary() == ["OBS"]
    # And the misheard word itself is never learned.
    assert "obese" not in profile.words


def test_a_repeat_long_after_the_miss_is_just_a_sentence(learning, monkeypatch):
    monkeypatch.setattr(config, "STT_RETRY_WINDOW_S", 20.0)
    profile = HearingProfile()
    profile.learn("run obese", rejected=True, uncertain=False, avg_logprob=-1.3, now=100.0)
    profile.learn("run OBS", rejected=False, uncertain=False, avg_logprob=-0.2, now=200.0)
    assert profile.vocabulary() == []


def test_doubtful_words_never_teach_anything(learning):
    profile = HearingProfile()
    for _ in range(5):
        profile.learn("open the escode", rejected=False, uncertain=True, avg_logprob=-0.7)
    assert profile.words == {}


def test_capitalised_spelling_wins(learning):
    profile = HearingProfile()
    profile.learn("open github", rejected=False, uncertain=False, avg_logprob=-0.2)
    profile.learn("open GitHub", rejected=False, uncertain=False, avg_logprob=-0.2)
    assert profile.vocabulary() == ["GitHub"]


def test_the_profile_survives_a_restart(learning):
    profile = HearingProfile()
    for _ in range(2):
        profile.learn("deploy Hyperion", rejected=False, uncertain=False, avg_logprob=-0.3)
    assert profile.save()
    again = HearingProfile()
    assert set(again.vocabulary()) == {"deploy", "Hyperion"}
    assert again.scored == 2


def test_a_damaged_profile_costs_what_was_learned_not_the_session(learning):
    learning.write_text('{"version": 1, "words": {"x": "not a pair"}, "logprob": []}')
    profile = HearingProfile()
    assert profile.vocabulary() == []


def test_the_vocabulary_is_bounded(learning, monkeypatch):
    monkeypatch.setattr(config, "STT_LEARN_MAX_WORDS", 10)
    profile = HearingProfile()
    for n in range(40):
        profile.learn(f"project{n}x", rejected=False, uncertain=False, avg_logprob=-0.2)
    assert len(profile.words) <= 10


# -- the confidence gate --------------------------------------------------------


def _scored(profile: HearingProfile, values: list[float]) -> None:
    for value in values:
        profile.learn("x", rejected=False, uncertain=False, avg_logprob=value)


def test_the_gate_stays_at_defaults_until_there_is_evidence(learning, monkeypatch):
    monkeypatch.setattr(config, "STT_ADAPT_MIN", 20)
    profile = HearingProfile()
    _scored(profile, [-0.55] * 5)
    assert profile.thresholds() == (config.STT_MIN_LOGPROB, config.STT_UNCERTAIN_LOGPROB)


def test_a_speaker_whisper_is_less_sure_of_gets_a_looser_gate(learning, monkeypatch):
    monkeypatch.setattr(config, "STT_ADAPT_MIN", 20)
    profile = HearingProfile()
    _scored(profile, [-0.75, -0.85, -0.7, -0.9] * 10)
    reject, doubt = profile.thresholds()
    assert reject < config.STT_MIN_LOGPROB
    assert doubt < config.STT_UNCERTAIN_LOGPROB
    # So their ordinary sentence is no longer flagged as unclear, and their
    # worse-than-usual one is no longer thrown away.
    normal = Transcript("open chrome", avg_logprob=-0.8, reject_below=reject, doubt_below=doubt)
    assert not normal.uncertain and not normal.rejected
    off_day = Transcript("open chrome", avg_logprob=-1.02, reject_below=reject, doubt_below=doubt)
    assert off_day.uncertain and not off_day.rejected


def test_a_speaker_near_only_the_doubt_line_keeps_the_reject_line(learning, monkeypatch):
    monkeypatch.setattr(config, "STT_ADAPT_MIN", 20)
    profile = HearingProfile()
    _scored(profile, [-0.55, -0.65, -0.5, -0.7] * 10)
    reject, doubt = profile.thresholds()
    assert reject == config.STT_MIN_LOGPROB
    assert doubt < config.STT_UNCERTAIN_LOGPROB


def test_the_gate_only_ever_loosens_and_only_so_far(learning, monkeypatch):
    monkeypatch.setattr(config, "STT_ADAPT_MIN", 5)
    monkeypatch.setattr(config, "STT_ADAPT_MAX_SHIFT", 0.35)
    clear = HearingProfile()
    _scored(clear, [-0.1] * 30)
    assert clear.thresholds() == (config.STT_MIN_LOGPROB, config.STT_UNCERTAIN_LOGPROB)

    mumbly = HearingProfile(learning.with_name("other.json"))
    _scored(mumbly, [-0.95, -0.4] * 30)
    reject, doubt = mumbly.thresholds()
    assert reject >= config.STT_MIN_LOGPROB - 0.35
    assert doubt >= config.STT_UNCERTAIN_LOGPROB - 0.35


def test_adaptation_can_be_switched_off(learning, monkeypatch):
    monkeypatch.setattr(config, "STT_ADAPTIVE_CONFIDENCE", False)
    monkeypatch.setattr(config, "STT_ADAPT_MIN", 1)
    profile = HearingProfile()
    _scored(profile, [-0.9] * 10)
    assert profile.thresholds() == (config.STT_MIN_LOGPROB, config.STT_UNCERTAIN_LOGPROB)


# -- the transcriber ------------------------------------------------------------


def test_learned_words_reach_the_decoding_prompt(learning):
    transcriber = Transcriber()
    for _ in range(2):
        transcriber.note_transcript(_clear("open Hyperion"))
    assert "Hyperion" in transcriber._prompt()
    # The previous utterance is still the very end of it.
    assert transcriber._prompt().endswith("open Hyperion")


def test_learned_words_are_never_crowded_out_by_the_program_list(learning):
    transcriber = Transcriber()
    transcriber.set_hints([f"Application Number {n}" for n in range(500)])
    for _ in range(2):
        transcriber.note_transcript(_clear("ping Hyperion"))
    prompt = transcriber._prompt()
    assert "Hyperion" in prompt
    assert len(prompt) <= config.STT_PROMPT_MAX_CHARS


def test_a_rejected_transcript_is_never_the_recent_context(learning):
    transcriber = Transcriber()
    transcriber.note_transcript(_clear("open notes"))
    transcriber.note_transcript(Transcript("elite the bill", avg_logprob=-1.6))
    assert transcriber._prompt().endswith("open notes")


def test_typed_text_teaches_the_recogniser_nothing(learning):
    """A typed command is a plain `str`: it never came through Whisper."""
    transcriber = Transcriber()
    for _ in range(3):
        transcriber.note_transcript("open Hyperion")
    assert transcriber.hearing.words == {}


def test_learning_off_means_no_profile_at_all(monkeypatch):
    monkeypatch.setattr(config, "STT_LEARN", False)
    assert Transcriber().hearing is None


# -- standby: the name is enough --------------------------------------------------


@pytest.mark.parametrize("said", ["EV", "E.V.?", "Evie", "hey EV", "Eevee", "heavy",
                                  "okay so EV you there", "Evi!"])
def test_the_name_or_something_like_it_summons(said):
    assert wake.summons(said).matched


@pytest.mark.parametrize("said", ["so anyway every time", "never mind", "open chrome",
                                  "", "even so"])
def test_ordinary_speech_does_not(said):
    assert not wake.summons(said).matched


def test_a_summons_carries_its_command():
    assert wake.summons("EV, open chrome").command == "open chrome"


def test_summons_ignores_wake_required_off(monkeypatch):
    """With the wake phrase off, `detect` matches everything; standby must not."""
    monkeypatch.setattr(config, "WAKE_REQUIRED", False)
    assert not wake.summons("so anyway the meeting moved").matched


class QuietUI:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, text):
        self.notes.append(text)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class NullTranscriber:
    def note_transcript(self, text):
        pass


def _sleeping(monkeypatch) -> ev_core.EV:
    monkeypatch.setattr(config, "WAKE_REQUIRED", True)
    assistant = ev_core.EV.__new__(ev_core.EV)
    assistant.ui = QuietUI()
    assistant.transcriber = NullTranscriber()
    assistant.session = Session()
    assistant.session.enter_standby()
    assistant.text_mode = False
    assistant.intents = []
    assistant.handled = []

    async def handle_intent(intent):
        assistant.intents.append(intent)

    async def handle(command, uncertain=False, **_):
        assistant.handled.append(command)

    assistant._handle_intent = handle_intent
    assistant.handle = handle
    return assistant


def test_saying_the_name_wakes_it_from_standby(monkeypatch):
    assistant = _sleeping(monkeypatch)
    heard = Transcript("Evie?")
    asyncio.run(assistant._route(heard, assistant._extract_command(heard)))
    assert assistant.intents == [Intent.RESUME]


def test_the_name_and_a_command_wakes_it_and_does_the_command(monkeypatch):
    assistant = _sleeping(monkeypatch)
    heard = Transcript("EV, open chrome")
    asyncio.run(assistant._route(heard, assistant._extract_command(heard)))
    assert assistant.session.engaged
    assert assistant.handled == ["open chrome"]
    assert assistant.intents == []


def test_room_talk_still_does_not(monkeypatch):
    assistant = _sleeping(monkeypatch)
    heard = Transcript("pass the salt")
    asyncio.run(assistant._route(heard, assistant._extract_command(heard)))
    assert assistant.intents == [] and assistant.handled == []
    assert assistant.session.in_standby
    assert any("standby" in note.lower() for note in assistant.ui.notes)
