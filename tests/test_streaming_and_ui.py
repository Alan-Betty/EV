"""Streaming reply extraction, and the UI/speech separation.

The streaming tests matter because the parser reads *incomplete* JSON: the
tool-call arguments arrive a few characters at a time and `json.loads` cannot
help until the last one lands. Getting this wrong means either speaking
garbage or losing the reply entirely.

The UI tests pin down the structural reason E.V. cannot read its own chrome
aloud: `ev.ui` renders, and returns nothing that could be spoken.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import pytest  # noqa: E402

import config  # noqa: E402
from ev import ui as ui_module  # noqa: E402
from ev.brain import _SentenceEmitter, partial_reply  # noqa: E402


# -- partial JSON ------------------------------------------------------------
def test_reads_a_complete_reply():
    assert partial_reply('{"reply": "Chrome\'s up."}') == "Chrome's up."


def test_reads_a_half_written_reply():
    assert partial_reply('{"reply": "Chrome\'s u') == "Chrome's u"


def test_returns_nothing_before_the_key_arrives():
    assert partial_reply('{"re') == ""
    assert partial_reply("") == ""


def test_decodes_escapes():
    assert partial_reply('{"reply": "line one\\nline two"}') == "line one\nline two"
    assert partial_reply('{"reply": "he said \\"hi\\""}') == 'he said "hi"'


def test_waits_for_an_escape_split_across_chunks():
    """A trailing backslash is half an escape, not a literal backslash."""
    assert partial_reply('{"reply": "line one\\') == "line one"


def test_decodes_unicode_escapes():
    assert partial_reply('{"reply": "caf\\u00e9"}') == "café"


def test_ignores_other_keys():
    assert partial_reply('{"app": "chrome", "reply": "Up."}') == "Up."


def test_grows_monotonically_as_chunks_arrive():
    """Feeding one character at a time must never lose or reorder text."""
    full = '{"reply": "One. Two. Three."}'
    seen = ""
    for size in range(1, len(full) + 1):
        current = partial_reply(full[:size])
        assert current.startswith(seen) or seen.startswith(current)
        seen = current
    assert seen == "One. Two. Three."


# -- sentence emission -------------------------------------------------------
def _collect(chunks, min_chars=12):
    spoken = []
    emitter = _SentenceEmitter(spoken.append)
    for chunk in chunks:
        emitter.feed(chunk)
    return spoken, emitter


def test_emits_a_sentence_as_soon_as_it_is_complete():
    spoken, _ = _collect(["Chrome's up and running.", "Chrome's up and running. Mice incoming."])
    assert spoken == ["Chrome's up and running."]


def test_does_not_emit_an_unfinished_sentence():
    spoken, _ = _collect(["Chrome's up and runn"])
    assert spoken == []


def test_flush_emits_the_tail():
    spoken, emitter = _collect(["Chrome's up and running. Mice inc"])
    emitter.flush("Chrome's up and running. Mice incoming.")
    assert spoken == ["Chrome's up and running.", "Mice incoming."]


def test_never_repeats_text():
    text = "First one here. Second one here. Third one here."
    spoken = []
    emitter = _SentenceEmitter(spoken.append)
    for size in range(1, len(text) + 1):
        emitter.feed(text[:size])
    emitter.flush(text)
    assert "".join(spoken).replace(" ", "") == text.replace(" ", "")


def test_holds_back_fragments_shorter_than_the_minimum(monkeypatch):
    """"E.V." parses as a finished sentence; it is not worth a round trip."""
    monkeypatch.setattr(config, "TTS_STREAM_MIN_CHARS", 12)
    spoken, _ = _collect(["E.V. is the assistant here."])
    assert spoken == []


def test_no_hook_is_a_no_op():
    emitter = _SentenceEmitter(None)
    emitter.feed("Anything at all. Really.")
    emitter.flush("Anything at all. Really.")  # must not raise


# -- UI / speech separation --------------------------------------------------
_RENDERERS = ["header", "hint", "user", "speech", "action", "note", "ok", "warn", "error", "rule"]


def test_every_renderer_returns_none():
    """Nothing the UI draws can be handed to the speaker, because it hands
    back nothing. This is the structural half of the no-labels guarantee."""
    ui = ui_module.UI(plain=True)
    for name in _RENDERERS:
        method = getattr(ui, name)
        arity = len(inspect.signature(method).parameters)
        assert method(*(["x"] * arity)) is None, name


def test_speech_does_not_mutate_what_it_is_given(capsys):
    ui = ui_module.UI(plain=True)
    text = "Chrome's up."
    ui.speech(text)
    assert text == "Chrome's up."  # unchanged for the caller to pass on
    assert "Chrome's up." in capsys.readouterr().out


def test_ui_falls_back_when_rich_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(ui_module, "_RICH_AVAILABLE", False)
    ui = ui_module.UI()
    assert not ui.rich
    ui.speech("Chrome's up.")
    out = capsys.readouterr().out
    assert "Chrome's up." in out
    assert "\x1b[" not in out  # no escape codes in plain mode


def test_plain_mode_emits_no_escape_codes(capsys):
    ui = ui_module.UI(plain=True)
    ui.header("groq llama", "en-US-GuyNeural", "voice")
    ui.user("open chrome")
    ui.action("open_app", "app=chrome")
    ui.warn("careful")
    assert "\x1b[" not in capsys.readouterr().out


def test_status_is_a_context_manager_even_without_rich():
    ui = ui_module.UI(plain=True)
    with ui.status("Thinking..."):
        pass  # must not raise


def test_status_tears_down_on_an_exception():
    ui = ui_module.UI(plain=True)
    with pytest.raises(ValueError):
        with ui.status("Thinking..."):
            raise ValueError("boom")


def test_prompt_reflects_session_state():
    ui = ui_module.UI(plain=True)
    assert ui.prompt(engaged=False, standby=True).startswith("standby")
    assert "." in ui.prompt(engaged=True, standby=False)


# -- console encoding --------------------------------------------------------
class _FakeStdout:
    """Stands in for a Windows console on a legacy code page."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding

    def isatty(self) -> bool:
        return False


def test_block_glyphs_are_dropped_on_a_legacy_code_page(monkeypatch):
    """A cp1252 console cannot encode the block banner and `print` raises.

    Regression: this crashed on the very first line of output, before the
    assistant had done anything at all.
    """
    monkeypatch.setattr(ui_module.sys, "stdout", _FakeStdout("cp1252"))
    ui = ui_module.UI()
    assert ui.header_art is ui_module.HEADER_ASCII
    assert ui.glyphs == ui_module.GLYPHS_ASCII
    # The whole banner and every glyph must survive the round trip.
    ("".join(ui.glyphs.values()) + ui.header_art).encode("cp1252")


def test_block_glyphs_are_kept_on_a_utf8_console(monkeypatch):
    monkeypatch.setattr(ui_module.sys, "stdout", _FakeStdout("utf-8"))
    ui = ui_module.UI()
    assert ui.header_art is ui_module.HEADER_UNICODE
    assert ui.glyphs == ui_module.GLYPHS_UNICODE


def test_an_unknown_encoding_falls_back_safely(monkeypatch):
    monkeypatch.setattr(ui_module.sys, "stdout", _FakeStdout("not-a-real-codec"))
    assert ui_module.UI().header_art is ui_module.HEADER_ASCII


def test_stdout_without_an_encoding_attribute_falls_back(monkeypatch):
    class Bare:
        def isatty(self):
            return False

    monkeypatch.setattr(ui_module.sys, "stdout", Bare())
    assert ui_module.UI().header_art is ui_module.HEADER_ASCII
