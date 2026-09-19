"""Gemini payload shape, model fallback, and cross-provider rate-limit rescue.

Offline: every request is answered by a stub transport, so the suite never
touches the network and needs no key that works.

The regression at the centre of this file is worth stating plainly, because it
was invisible from every angle except a real request. `to_gemini_tools()`
filtered the keys of the `properties` map against the list of schema keywords,
and argument names are not schema keywords - so every tool reached Gemini with
no properties at all. The tool *names* were all still correct, which is why
name-level tests stayed green while the provider could not perform a single
action. Gemini answered `required[0]: property is not defined`, which was true.
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
from ev.brain import Brain, BrainError, _gemini_retry_after  # noqa: E402
from ev.tts import clean_for_speech  # noqa: E402
from tools.schemas import TOOL_SPECS, to_gemini_tools  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _brain(monkeypatch, handler, provider: str = "gemini") -> Brain:
    """A Brain wired to a stub transport, with both providers keyed."""
    monkeypatch.setattr(config, "LLM_PROVIDER", provider)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setattr(config, "GROQ_API_KEY", "groq-test-key")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Brain(client)


def _function_call(name: str, args: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"parts": [{"functionCall": {"name": name, "args": args}}]},
                    "finishReason": "STOP",
                }
            ]
        },
    )


# ---------------------------------------------------------------------------
# payload shape
# ---------------------------------------------------------------------------
def test_every_declared_property_survives_translation():
    """The bug: `properties` came out empty for all thirteen tools."""
    declarations = {
        decl["name"]: decl for decl in to_gemini_tools()[0]["functionDeclarations"]
    }
    assert declarations, "no function declarations at all"

    for spec in TOOL_SPECS:
        declared = set(spec["parameters"].get("properties", {}))
        got = set(declarations[spec["name"]]["parameters"].get("properties", {}))
        assert got == declared, f"{spec['name']} lost {declared - got}"


def test_nothing_named_in_required_is_undefined():
    """The exact invariant Gemini enforces, and the exact 400 it used to send."""
    for decl in to_gemini_tools()[0]["functionDeclarations"]:
        params = decl["parameters"]
        defined = set(params.get("properties", {}))
        for name in params.get("required", []):
            assert name in defined, (
                f"{decl['name']}: required {name!r} is not a defined property - "
                "this is the 'property is not defined' 400"
            )


def test_property_descriptions_and_enums_come_through():
    """Recursing into the values, not just keeping the keys."""
    declarations = {
        decl["name"]: decl for decl in to_gemini_tools()[0]["functionDeclarations"]
    }
    engine = declarations["web_search"]["parameters"]["properties"]["engine"]
    assert engine["type"] == "string"
    assert "google" in engine["enum"]
    assert engine["description"]


def test_unsupported_keys_are_still_stripped():
    """The subset rule the stripper exists for has not been loosened."""
    from tools.schemas import _strip_unsupported

    cleaned = _strip_unsupported(
        {
            "type": "object",
            "additionalProperties": False,      # not in Gemini's subset
            "$schema": "http://json-schema.org/",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "keep me",
                    "minLength": 3,             # not in Gemini's subset
                }
            },
            "required": ["path"],
        }
    )
    assert set(cleaned) == {"type", "properties", "required"}
    assert set(cleaned["properties"]["path"]) == {"type", "description"}
    assert cleaned["required"] == ["path"]


def test_a_property_named_like_a_keyword_is_not_mistaken_for_one():
    """`items` and `type` are legal argument names as well as schema keys."""
    from tools.schemas import _strip_unsupported

    cleaned = _strip_unsupported(
        {
            "type": "object",
            "properties": {
                "items": {"type": "string", "description": "what to fetch"},
                "type": {"type": "string", "description": "which kind"},
            },
            "required": ["items"],
        }
    )
    assert set(cleaned["properties"]) == {"items", "type"}
    assert cleaned["properties"]["items"]["description"] == "what to fetch"


def test_the_request_gemini_receives_is_well_formed(monkeypatch):
    """System prompt, history and tools all land in the fields Gemini reads."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["_headers"] = dict(request.headers)
        return _function_call("open_app", {"app": "notepad"})

    brain = _brain(monkeypatch, handler)
    brain.remember("hello", "Hi.", observation="Launched nothing")
    call = asyncio.run(brain.decide("open notepad"))

    assert call.name == "open_app"
    assert call.arguments == {"app": "notepad"}

    assert seen["systemInstruction"]["parts"][0]["text"]
    assert seen["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert seen["_headers"]["x-goog-api-key"] == "gemini-test-key"

    roles = [part["role"] for part in seen["contents"]]
    assert roles[-1] == "user"
    # The observation is replayed as input, never as a model turn - the
    # speech-purity boundary holds on this provider too.
    model_turns = [
        c["parts"][0]["text"] for c in seen["contents"] if c["role"] == "model"
    ]
    assert model_turns == ["Hi."]
    assert not any("Result of that action" in text for text in model_turns)


# ---------------------------------------------------------------------------
# response handling
# ---------------------------------------------------------------------------
def test_an_empty_candidate_from_max_tokens_becomes_speech(monkeypatch):
    """A thinking model can spend the whole budget before writing a part.

    That candidate is finished and valid and empty. Reporting it as a
    malformed response ended the turn with an error; the plain reply turns it
    into words instead.
    """
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "tools" in body:
            return httpx.Response(
                200, json={"candidates": [{"finishReason": "MAX_TOKENS", "content": {}}]}
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "Paris."}]}, "finishReason": "STOP"}
                ]
            },
        )

    brain = _brain(monkeypatch, handler)
    call = asyncio.run(brain.decide("capital of France"))

    assert call.name == "chat"
    assert call.arguments["reply"] == "Paris."
    # The retry drops the tools, which is the thing that was failing.
    assert "tools" not in calls[1]
    assert calls[1]["generationConfig"]["maxOutputTokens"] >= 1024


def test_a_blocked_prompt_still_reports_itself(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"candidates": [{}], "promptFeedback": {"blockReason": "SAFETY"}}
        )

    brain = _brain(monkeypatch, handler)
    with pytest.raises(BrainError, match="SAFETY"):
        asyncio.run(brain.decide("something"))


def test_prose_that_is_really_a_tool_call_is_rescued(monkeypatch):
    """Same salvage the Groq path has; Gemini improvises in the same way."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"name": "open_app", '
                                    '"arguments": {"app": "chrome"}}'
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    brain = _brain(monkeypatch, handler)
    call = asyncio.run(brain.decide("open chrome"))
    assert call.name == "open_app"
    assert call.arguments == {"app": "chrome"}


# ---------------------------------------------------------------------------
# model availability
# ---------------------------------------------------------------------------
def test_a_404_model_falls_down_the_ladder(monkeypatch):
    """Listing is not proof: a model can be catalogued and still 404.

    `gemini-2.5-flash` does exactly that on newer keys - "no longer available
    to new users" - so the ladder is walked with real requests.
    """
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(
        config, "GEMINI_MODEL_FALLBACKS", ["gemini-2.5-flash", "gemini-flash-latest"]
    )
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/")[-1].split(":")[0]
        tried.append(model)
        if model == "gemini-2.5-flash":
            return httpx.Response(404, json={"error": {"message": "no longer available"}})
        return httpx.Response(200, json={"candidates": [{}]})

    brain = _brain(monkeypatch, handler)
    chosen = asyncio.run(brain.verify_model())

    assert chosen == "gemini-flash-latest"
    assert config.GEMINI_MODEL == "gemini-flash-latest"
    assert tried == ["gemini-2.5-flash", "gemini-flash-latest"]


def test_the_vision_model_follows_the_brain_off_a_dead_model(monkeypatch):
    """Otherwise E.V. talks fine and goes blind.

    `GEMINI_VISION_MODEL` is bound at import from `GEMINI_MODEL`. When the
    ladder moves the brain after a 404, a derived vision model has to move
    with it - it is pointed at the model that just 404ed otherwise, and every
    screenshot fails while everything else works.
    """
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(config, "GEMINI_VISION_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(config, "GEMINI_VISION_MODEL_PINNED", False)
    monkeypatch.setattr(config, "GEMINI_MODEL_FALLBACKS", ["gemini-flash-latest"])

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/")[-1].split(":")[0]
        if model == "gemini-2.5-flash":
            return httpx.Response(404, json={"error": {"message": "gone"}})
        return httpx.Response(200, json={"candidates": [{}]})

    brain = _brain(monkeypatch, handler)
    asyncio.run(brain.verify_model())

    assert config.GEMINI_VISION_MODEL == "gemini-flash-latest"


def test_a_pinned_vision_model_is_left_alone(monkeypatch):
    """An explicit EV_GEMINI_VISION_MODEL is a choice, not a default."""
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(config, "GEMINI_VISION_MODEL", "gemini-3-pro-image")
    monkeypatch.setattr(config, "GEMINI_VISION_MODEL_PINNED", True)
    monkeypatch.setattr(config, "GEMINI_MODEL_FALLBACKS", ["gemini-flash-latest"])

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/")[-1].split(":")[0]
        if model == "gemini-2.5-flash":
            return httpx.Response(404, json={"error": {"message": "gone"}})
        return httpx.Response(200, json={"candidates": [{}]})

    brain = _brain(monkeypatch, handler)
    asyncio.run(brain.verify_model())

    assert config.GEMINI_VISION_MODEL == "gemini-3-pro-image"


def test_a_busy_model_is_not_treated_as_a_missing_one(monkeypatch):
    """429 and 503 mean the model exists. Walking past it lands on a worse one."""
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.setattr(config, "GEMINI_MODEL_FALLBACKS", ["gemini-2.5-flash-lite"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "high demand"}})

    brain = _brain(monkeypatch, handler)
    assert asyncio.run(brain.verify_model()) == "gemini-flash-latest"


def test_no_usable_model_names_the_ones_that_were_tried(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-ghost")
    monkeypatch.setattr(config, "GEMINI_MODEL_FALLBACKS", ["gemini-phantom"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "gone"}})

    brain = _brain(monkeypatch, handler)
    with pytest.raises(BrainError) as excinfo:
        asyncio.run(brain.verify_model())
    assert "gemini-ghost" in str(excinfo.value)
    assert "gemini-phantom" in str(excinfo.value)


# ---------------------------------------------------------------------------
# rate limits and failover
# ---------------------------------------------------------------------------
def test_gemini_states_its_retry_delay_in_the_body():
    """Groq uses a header; Gemini uses a RetryInfo detail. Both are read."""
    body = json.dumps(
        {"error": {"code": 429, "details": [{"@type": "RetryInfo", "retryDelay": "31s"}]}}
    )
    assert _gemini_retry_after(body) == 31.0
    assert _gemini_retry_after(json.dumps({"error": {"details": [{"retryDelay": "250ms"}]}})) == 0.25
    assert _gemini_retry_after("not json") is None
    assert _gemini_retry_after(json.dumps({"error": {}})) is None


def test_a_rate_limit_is_flagged_rather_than_just_raised(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", False)
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_RETRIES", 0)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "quota"}})

    brain = _brain(monkeypatch, handler)
    with pytest.raises(BrainError) as excinfo:
        asyncio.run(brain.decide("open notepad"))
    assert excinfo.value.rate_limited is True
    # Spoken aloud if it gets that far, so it has to be English.
    assert clean_for_speech(str(excinfo.value)) == str(excinfo.value)
    assert "{" not in str(excinfo.value)


def test_a_gemini_rate_limit_falls_over_to_groq(monkeypatch):
    """The whole point: the session carries on instead of asking the user to wait."""
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_RETRIES", 0)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if "generativelanguage" in request.url.host:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "open_app",
                                        "arguments": '{"app": "notepad"}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    brain = _brain(monkeypatch, handler)
    call = asyncio.run(brain.decide("open notepad"))

    assert call.name == "open_app"
    assert call.arguments == {"app": "notepad"}
    assert any("generativelanguage" in host for host in hosts)
    assert any("groq" in host for host in hosts)
    # One turn only. A single 429 must not silently relocate the session.
    assert brain.provider == "gemini"


def test_a_groq_rate_limit_falls_over_to_gemini(monkeypatch):
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_RETRIES", 0)
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if "groq" in request.url.host:
            return httpx.Response(429, json={"error": {"message": "TPM exceeded"}})
        return _function_call("chat", {"reply": "Still here."})

    brain = _brain(monkeypatch, handler, provider="groq")
    call = asyncio.run(brain.decide("you there"))

    assert call.name == "chat"
    assert call.arguments == {"reply": "Still here."}
    assert any("groq" in host for host in hosts)
    assert any("generativelanguage" in host for host in hosts)
    assert brain.provider == "groq"


def test_failover_does_not_bounce_back_and_forth(monkeypatch):
    """Both providers rate limited is one hop, then an honest error."""
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_RETRIES", 0)
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        return httpx.Response(429, json={"error": {"message": "quota"}})

    brain = _brain(monkeypatch, handler, provider="groq")
    with pytest.raises(BrainError):
        asyncio.run(brain.decide("open notepad"))

    assert len(attempts) == 2, f"expected one hop, got {attempts}"
    assert brain.provider == "groq"


def test_failover_is_off_without_a_second_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_RETRIES", 0)
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        return httpx.Response(429, json={"error": {"message": "quota"}})

    brain = _brain(monkeypatch, handler, provider="groq")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    with pytest.raises(BrainError):
        asyncio.run(brain.decide("open notepad"))
    assert len(attempts) == 1


def test_a_busy_model_takes_the_same_route_as_a_rate_limit(monkeypatch):
    """503 "high demand" is a wait-or-ask-elsewhere problem, like a 429."""
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_RETRIES", 0)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if "generativelanguage" in request.url.host:
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Go on then."}}]}
        )

    brain = _brain(monkeypatch, handler)
    call = asyncio.run(brain.decide("hello"))
    assert call.name == "chat"
    assert any("groq" in host for host in hosts)


def test_an_unknown_provider_is_refused_at_construction(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    with pytest.raises(BrainError, match="groq or gemini"):
        Brain(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
