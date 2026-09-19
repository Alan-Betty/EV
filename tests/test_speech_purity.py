"""The speaker must never be handed a label.

This is the regression suite for the bug where E.V. read "Spoke:" out loud.
It had two halves and both are covered here:

* the *source* - a tool observation stored as an assistant turn, which taught
  the model to imitate the prefix (`test_history_*`),
* the *boundary* - whatever the model emits, nothing label-shaped survives
  `clean_for_speech` (`test_strips_*`).

The keep-cases matter as much as the strip-cases. A stripper aggressive enough
to eat "Spoke to your mother" would be its own bug.
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
from ev.brain import Brain, Turn  # noqa: E402
from ev.tts import clean_for_speech, strip_meta_labels  # noqa: E402
from tools import chat, dispatch  # noqa: E402


# -- the boundary ------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "Spoke: Chrome's up.",
        "spoke: Chrome's up.",
        "SPOKE: Chrome's up.",
        "E.V.: Chrome's up.",
        "EV: Chrome's up.",
        "E.V. > Chrome's up.",
        "[E.V.] Chrome's up.",
        "(assistant) Chrome's up.",
        "<system> Chrome's up.",
        "Response: Chrome's up.",
        "Reply: Chrome's up.",
        "Answer: Chrome's up.",
        "Assistant: Chrome's up.",
        "Output: Chrome's up.",
        "Result: Chrome's up.",
        "Status: Chrome's up.",
        "Thinking: Chrome's up.",
        "Message: Chrome's up.",
        "Spoke: E.V.: Chrome's up.",
        "**E.V.:** Chrome's up.",
        '"Chrome\'s up."',
        '{"reply": "Chrome\'s up."}',
        "$ git status\nexit=0\nChrome's up.",
    ],
)
def test_strips_every_meta_label(raw):
    assert clean_for_speech(raw) == "Chrome's up."


@pytest.mark.parametrize(
    "raw",
    [
        # Each opens with a label *word* but is an ordinary sentence. The
        # separator is what distinguishes a label, so none of these are touched.
        "Spoke to your mother about it.",
        "Status report looks clean.",
        "You need a break.",
        "Text me when it's done.",
        "Answering that takes a minute.",
        "Result of the build: clean.",
        "Note that the build is green.",
        "Chrome's up.",
        "Three thirteen point two. Modern of you.",
    ],
)
def test_keeps_ordinary_sentences_intact(raw):
    assert clean_for_speech(raw) == raw


def test_strip_is_idempotent():
    once = strip_meta_labels("Spoke: E.V.: hello")
    assert strip_meta_labels(once) == once == "hello"


def test_label_only_input_produces_no_speech():
    # Nothing left to say is correct; saying the label would not be.
    assert clean_for_speech("Spoke:") == ""
    assert clean_for_speech("   ") == ""


# -- the source --------------------------------------------------------------
def test_chat_detail_carries_no_label():
    """`chat` used to report 'Spoke: <text>', which is what poisoned history."""
    result = chat(reply="Chrome's up.")
    assert result.speech == "Chrome's up."
    assert "Spoke:" not in result.detail
    assert result.detail == result.speech


def test_dispatch_chat_detail_carries_no_label():
    result = dispatch("chat", {"reply": "Mm-hm."})
    assert "Spoke" not in result.detail


def test_history_keeps_observations_out_of_the_assistant_role():
    """A tool observation must never be replayed as something E.V. *said*.

    This is the actual root cause: an assistant turn reading "$ git status
    exit=0" is a worked example, and the model copies worked examples.
    """
    brain = Brain.__new__(Brain)  # no network, no key check
    brain.provider = "groq"
    brain.history = []
    brain.remember("run git status", "Clean tree.", "$ git status\nexit=0\nnothing to commit")

    messages = brain._groq_messages("and now?", "")
    assistant = [m["content"] for m in messages if m["role"] == "assistant"]
    assert assistant == ["Clean tree."]
    assert all("exit=0" not in text for text in assistant)

    # The observation is still available to the model, just not as its own words.
    others = " ".join(m["content"] for m in messages if m["role"] != "assistant")
    assert "exit=0" in others


def test_observation_is_length_capped():
    brain = Brain.__new__(Brain)
    brain.provider = "groq"
    brain.history = []
    brain.remember("ls", "Lots of files.", "x" * 10_000)
    assert len(brain.history[0].observation) == config.HISTORY_OBSERVATION_CHARS


def test_turn_defaults_to_no_observation():
    turn = Turn("hi", "Hey.")
    assert turn.observation == ""


# ---------------------------------------------------------------------------
# Typographic punctuation
# ---------------------------------------------------------------------------
def test_curly_punctuation_is_flattened_to_ascii():
    """The Windows console is cp1252 and cannot encode a curly apostrophe.

    The same cleaned string is drawn by the UI and handed to the speaker - that
    is deliberate - so a character the terminal cannot render shows up as
    "That?s a marathon" even though the audio was perfectly fine. A warm,
    conversational register produces these constantly, so this stopped being
    cosmetic the moment the voice stopped being clipped.
    """
    assert clean_for_speech("That\u2019s a marathon") == "That's a marathon"
    assert clean_for_speech("Wait \u2014 no") == "Wait - no"
    assert clean_for_speech("Hmm\u2026 maybe") == "Hmm... maybe"
    assert clean_for_speech("caf\u00e9\u00a0open") == "caf\u00e9 open"


def test_a_reply_wrapped_in_curly_quotes_is_still_unwrapped():
    """Flattening runs first, so the unwrapping below it sees ASCII quotes."""
    assert clean_for_speech("\u201cChrome's up.\u201d") == "Chrome's up."


def test_flattening_leaves_real_words_alone():
    """Accented letters are speech. Only punctuation is being normalised."""
    assert clean_for_speech("Caf\u00e9 na\u00efve r\u00e9sum\u00e9") == "Caf\u00e9 na\u00efve r\u00e9sum\u00e9"


def test_everything_cleaned_survives_a_cp1252_console():
    """The actual failure, asserted directly rather than by proxy."""
    samples = [
        "That\u2019s a marathon. Want a coffee, or are we powering through?",
        "\u201cDone\u201d \u2014 more or less\u2026",
        "Nineteen. That\u2019s a lot of hours.",
    ]
    for sample in samples:
        cleaned = clean_for_speech(sample)
        # Raises UnicodeEncodeError if anything curly survived.
        cleaned.encode("cp1252")
