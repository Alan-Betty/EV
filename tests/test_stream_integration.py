"""End-to-end streaming: SSE frames in, spoken sentences out.

These drive the real `Brain` against a mocked Groq transport, so the SSE
parsing, the partial-JSON extraction and the sentence hook are all exercised
together. A unit test of any one of them would not have caught a mismatch
between them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["EV_TTS_ENABLED"] = "false"

import httpx  # noqa: E402
import pytest  # noqa: E402

import config  # noqa: E402
from ev.brain import Brain, BrainError  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402


def _frame(**delta) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"


def _tool_stream(name: str, argument_chunks: list[str]) -> bytes:
    """An SSE body that dribbles tool-call arguments out a few chars at a time."""
    body = _frame(tool_calls=[{"index": 0, "function": {"name": name, "arguments": ""}}])
    for chunk in argument_chunks:
        body += _frame(tool_calls=[{"index": 0, "function": {"arguments": chunk}}])
    return (body + "data: [DONE]\n\n").encode()


def _brain(body: bytes, status: int = 200) -> tuple[Brain, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body))
    )
    brain = Brain.__new__(Brain)
    brain.provider = "groq"
    brain.history = []
    brain._client = client
    brain._owns_client = False
    return brain, client


def _run(brain, client, hook, command="say hi"):
    async def go():
        try:
            return await brain.decide(command, on_sentence=hook)
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_sentences_are_spoken_while_the_reply_is_still_arriving():
    chunks = ['{"reply": "Chrome', "'s up and running. ", "Mice incoming.", '"}']
    brain, client = _brain(_tool_stream("chat", chunks))

    spoken: list[str] = []
    call = _run(brain, client, spoken.append)

    assert call.name == "chat"
    assert call.arguments["reply"] == "Chrome's up and running. Mice incoming."
    # The first sentence went out before the last chunk arrived - that is the
    # entire point of the streaming path.
    assert spoken[0] == "Chrome's up and running."
    assert " ".join(spoken) == "Chrome's up and running. Mice incoming."


def test_nothing_is_dropped_between_stream_and_final_call():
    chunks = ['{"reply": "One here. Two here. Three here."}']
    brain, client = _brain(_tool_stream("chat", chunks))
    spoken: list[str] = []
    call = _run(brain, client, spoken.append)
    assert " ".join(spoken) == call.arguments["reply"]


def test_a_side_effecting_tool_never_speaks_early():
    """Announcing "Chrome's up" before Chrome is up would be a lie."""
    chunks = ['{"app": "chrome"', "}"]
    brain, client = _brain(_tool_stream("open_app", chunks))
    spoken: list[str] = []
    call = _run(brain, client, spoken.append)

    assert call.name == "open_app"
    assert call.arguments == {"app": "chrome"}
    assert spoken == [], "only chat may start speaking mid-generation"


def test_streamed_reply_reaches_the_speaker_label_free():
    """A model that still emits a label must not get one past the boundary."""
    chunks = ['{"reply": "Spoke: Chrome\'s up and running. All good here."}']
    brain, client = _brain(_tool_stream("chat", chunks))
    spoken: list[str] = []
    _run(brain, client, lambda s: spoken.append(clean_for_speech(s)))
    assert spoken[0] == "Chrome's up and running."
    assert not any("Spoke" in s for s in spoken)


def test_prose_instead_of_a_tool_call_still_speaks():
    body = (
        _frame(content="Above my pay grade. ")
        + _frame(content="Want me to search it?")
        + "data: [DONE]\n\n"
    ).encode()
    brain, client = _brain(body)
    spoken: list[str] = []
    call = _run(brain, client, spoken.append)

    assert call.name == "chat"
    assert "pay grade" in call.arguments["reply"]
    assert spoken, "a prose fallback must still be spoken"


def test_malformed_frames_are_skipped_not_fatal():
    body = (
        "data: {not json at all\n\n"
        + "data: \n\n"
        + ": a comment line\n\n"
        + _frame(tool_calls=[{"index": 0, "function": {"name": "chat", "arguments": '{"reply": "Still here."}'}}])
        + "data: [DONE]\n\n"
    ).encode()
    brain, client = _brain(body)
    call = _run(brain, client, [].append)
    assert call.arguments["reply"] == "Still here."


def test_an_http_error_surfaces_as_a_brain_error():
    brain, client = _brain(b'{"error": "nope"}', status=401)
    with pytest.raises(BrainError, match="key"):
        _run(brain, client, [].append)


def test_streaming_is_skipped_when_no_hook_is_given():
    """Without a sentence hook there is nothing to stream *to*, so the
    cheaper non-streaming path is used and must still work."""
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "chat", "arguments": '{"reply": "Mm-hm."}'}}
                        ]
                    }
                }
            ]
        }
    ).encode()
    brain, client = _brain(body)

    async def go():
        try:
            return await brain.decide("thanks")  # no on_sentence
        finally:
            await client.aclose()

    call = asyncio.run(go())
    assert call.arguments["reply"] == "Mm-hm."


def test_streaming_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "chat", "arguments": '{"reply": "Fine."}'}}
                        ]
                    }
                }
            ]
        }
    ).encode()
    brain, client = _brain(body)
    spoken: list[str] = []
    call = _run(brain, client, spoken.append)
    assert call.arguments["reply"] == "Fine."
    assert spoken == [], "with streaming off nothing should be emitted early"
