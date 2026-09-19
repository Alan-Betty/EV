"""Living within a free tier: token buckets, tool subsets and vision budget.

Offline. Every request is answered by a stub transport, so none of this needs
a key that works.

Three separate economies are pinned here, and they are separate on purpose:

* Groq meters tokens per minute per *model*, so a 429 is news about one
  bucket rather than about the key. Rotating to the next model is a bigger,
  cheaper win than reaching for the other provider, whose free tier is
  metered per day.
* The tool schema is ~2830 tokens of the ~4400 a real turn spends, and most
  of it is irrelevant to the utterance paying for it. Filtering it per
  utterance is the largest single lever on how many commands fit in a minute.
* A vision step costs about 1900 against that same budget and the charge does
  not shrink with the frame, so the only economy available to a screen task
  is knowing when to stop.
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
from tools.schemas import (  # noqa: E402
    CORE_TOOLS,
    TOOL_NAMES,
    select_tools,
    to_gemini_tools,
    to_openai_tools,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
REQUESTS: list[httpx.Request] = []


def _client(handler) -> httpx.AsyncClient:
    def capture(request: httpx.Request) -> httpx.Response:
        REQUESTS.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(capture))


def _tool_response(name: str, arguments: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                }
                            }
                        ]
                    }
                }
            ]
        },
    )


def _rate_limited() -> httpx.Response:
    # A stated delay far beyond the wait ceiling, so `_post` gives up rather
    # than sleeping through the test.
    return httpx.Response(
        429,
        headers={"retry-after": "600"},
        json={"error": {"message": "Rate limit reached", "type": "rate_limit"}},
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _models(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": n} for n in names]})


@pytest.fixture(autouse=True)
def _clean():
    REQUESTS.clear()
    yield
    REQUESTS.clear()


# ---------------------------------------------------------------------------
# tool subsetting
# ---------------------------------------------------------------------------
def test_the_core_tools_are_offered_whatever_was_said():
    """A transcript matching nothing still has to reach `chat`.

    This is the property that makes a wrong guess survivable rather than
    silent: routing decides which extras to pay for, never whether E.V. can
    answer at all.
    """
    for utterance in ("", "hmm", "asdfgh", "tell me a joke", "take five"):
        assert set(CORE_TOOLS) <= set(select_tools(utterance))


def test_selection_is_always_a_real_subset_of_the_real_tools():
    for utterance in ("open notepad", "tidy my desktop", "click save"):
        chosen = select_tools(utterance)
        assert set(chosen) <= set(TOOL_NAMES)
        assert len(chosen) == len(set(chosen))


def test_the_utterance_pulls_in_the_tool_it_needs():
    assert "file_manager" in select_tools("delete the pdfs in my downloads")
    assert "terminal_command" in select_tools("run git status")
    assert "browser_task" in select_tools("open gmail and summarise my inbox")
    assert "backlog" in select_tools("what is still on the backlog")


def test_a_gui_word_offers_the_whole_gui_family():
    """The model's job on a GUI request is to choose between one shot and a
    loop, and between the pointer and the keyboard. Showing it two of the
    four turns that choice into a guess.
    """
    chosen = set(select_tools("click the save button"))
    assert {
        "take_screenshot",
        "mouse_action",
        "keyboard_action",
        "screen_task",
    } <= chosen


def test_a_memory_word_offers_the_whole_memory_family():
    chosen = set(select_tools("remind me to call the dentist"))
    assert {"remember_fact", "recall_fact", "manage_todo"} <= chosen


def test_a_tool_with_no_trigger_words_is_not_offered_to_everything():
    """The empty-alternation trap. An empty alternation compiles to a pattern
    that matches the empty string at the first word boundary of anything at
    all, so a tool whose trigger list is empty was silently offered on every
    single utterance - the exact opposite of what the filter is for.
    """
    plain = set(select_tools("open notepad"))
    assert "recall_fact" not in plain
    assert "manage_todo" not in plain
    assert "file_manager" not in plain


def test_the_subset_actually_removes_most_of_the_schema():
    """The whole point is tokens, so measure tokens rather than trusting that
    fewer names means a smaller payload.
    """
    full = len(json.dumps(to_openai_tools()))
    small = len(json.dumps(to_openai_tools(select_tools("open notepad"))))
    assert small < full // 2


def test_an_unknown_name_falls_back_to_everything_rather_than_nothing():
    """The selector is a heuristic. A typo in it must cost tokens, never the
    assistant's ability to act.
    """
    assert to_openai_tools(["no-such-tool"]) == to_openai_tools()
    assert to_gemini_tools(["no-such-tool"]) == to_gemini_tools()


def test_both_providers_are_offered_the_same_selection():
    """Failover re-runs the utterance in the other dialect. A request that
    needed `file_manager` on Groq must still find it on Gemini.
    """
    names = select_tools("tidy my desktop")
    openai_names = {t["function"]["name"] for t in to_openai_tools(names)}
    gemini_names = {
        d["name"] for d in to_gemini_tools(names)[0]["functionDeclarations"]
    }
    assert openai_names == gemini_names == set(names)


def test_switching_the_subset_off_sends_everything(monkeypatch):
    monkeypatch.setattr(config, "TOOL_SUBSET_ENABLED", False)

    async def go():
        client = _client(lambda r: _tool_response("open_app", {"app": "notepad"}))
        brain = Brain(client)
        brain.provider = "groq"
        await brain.decide("open notepad")
        await client.aclose()

    _run(go())
    body = json.loads(REQUESTS[-1].content)
    assert {t["function"]["name"] for t in body["tools"]} == set(TOOL_NAMES)


def test_a_rejected_tool_call_is_retried_with_the_whole_schema(monkeypatch):
    """Two things put us on this rung and it answers both at once: the model
    could not express itself in the tools it was shown, or it wanted no tool
    at all. Distinguishing them would cost a round trip and have the same fix
    either way.
    """
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    seen: list[set[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({t["function"]["name"] for t in body.get("tools", [])})
        if len(seen) == 1:
            return httpx.Response(
                400, json={"error": {"code": "tool_use_failed", "message": "nope"}}
            )
        return _tool_response("chat", {"reply": "done"})

    async def go():
        client = _client(handler)
        brain = Brain(client)
        brain.provider = "groq"
        call = await brain.decide("open notepad")
        await client.aclose()
        return call

    call = _run(go())
    assert call.name == "chat"
    assert seen[0] != set(TOOL_NAMES)  # first try was the subset
    assert seen[1] == set(TOOL_NAMES)  # retry showed everything
    assert json.loads(REQUESTS[-1].content)["tool_choice"] == "auto"


# ---------------------------------------------------------------------------
# per-model token buckets
# ---------------------------------------------------------------------------
def test_a_rate_limit_moves_to_the_next_bucket_and_stays_there(monkeypatch):
    """Sticky, unlike provider failover. The bucket just abandoned needs a
    full minute to refill, so hopping back on the next utterance would land
    straight back in the wall.
    """
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    monkeypatch.setattr(config, "GROQ_MODEL", "model-a")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", False)

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "model-a":
            return _rate_limited()
        return _tool_response("open_app", {"app": "notepad"})

    async def go():
        client = _client(handler)
        brain = Brain(client)
        brain.provider = "groq"
        brain._groq_rotation = ["model-a", "model-b"]
        first = await brain.decide("open notepad")
        second = await brain.decide("open notepad")
        await client.aclose()
        return first, second, brain

    first, second, brain = _run(go())
    assert first.name == "open_app" and second.name == "open_app"
    assert brain.groq_model == "model-b"
    models = [json.loads(r.content)["model"] for r in REQUESTS]
    assert models == ["model-a", "model-b", "model-b"]


def test_rotation_wraps_so_a_refilled_bucket_comes_back_round(monkeypatch):
    monkeypatch.setattr(config, "GROQ_MODEL", "model-a")

    async def go():
        client = _client(lambda r: _tool_response("chat", {"reply": "hi"}))
        brain = Brain(client)
        brain._groq_rotation = ["model-a", "model-b"]
        seen = [brain.groq_model]
        for _ in range(3):
            brain._rotate_groq_model()
            seen.append(brain.groq_model)
        await client.aclose()
        return seen

    assert _run(go()) == ["model-a", "model-b", "model-a", "model-b"]


def test_one_bucket_cannot_rotate(monkeypatch):
    monkeypatch.setattr(config, "GROQ_MODEL", "only")

    async def go():
        client = _client(lambda r: _tool_response("chat", {"reply": "hi"}))
        brain = Brain(client)
        brain._groq_rotation = ["only"]
        moved = brain._rotate_groq_model()
        await client.aclose()
        return moved, brain.groq_model

    moved, model = _run(go())
    assert moved is False
    assert model == "only"


def test_the_other_provider_is_only_asked_once_every_bucket_is_empty(monkeypatch):
    """One hop here costs nothing; one hop there spends a scarce daily
    request. So the buckets go first, all of them, and Gemini is the last
    thing between the user and being told to come back later.
    """
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    monkeypatch.setattr(config, "GROQ_MODEL", "model-a")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-gemini")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", True)

    def handler(request: httpx.Request) -> httpx.Response:
        if ":generateContent" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "chat",
                                            "args": {"reply": "rescued"},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        return _rate_limited()

    async def go():
        client = _client(handler)
        brain = Brain(client)
        brain.provider = "groq"
        brain._groq_rotation = ["model-a", "model-b", "model-c"]
        call = await brain.decide("open notepad")
        await client.aclose()
        return call

    call = _run(go())
    assert call.name == "chat" and call.arguments["reply"] == "rescued"
    groq_models = [
        json.loads(r.content)["model"]
        for r in REQUESTS
        if "chat/completions" in str(r.url)
    ]
    assert groq_models == ["model-a", "model-b", "model-c"]


def test_with_no_other_provider_the_rate_limit_is_finally_admitted(monkeypatch):
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    monkeypatch.setattr(config, "GROQ_MODEL", "model-a")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", False)

    async def go():
        client = _client(lambda r: _rate_limited())
        brain = Brain(client)
        brain.provider = "groq"
        brain._groq_rotation = ["model-a", "model-b"]
        with pytest.raises(BrainError) as excinfo:
            await brain.decide("open notepad")
        await client.aclose()
        return excinfo.value

    exc = _run(go())
    assert exc.rate_limited is True
    # Spoken aloud, so it has to be English rather than a JSON error body.
    assert "{" not in str(exc)


def test_a_streamed_rate_limit_still_reaches_the_rotation(monkeypatch):
    """It used not to. `decide` re-raised anything that was not a
    `tool_failure`, so a 429 raised while streaming escaped the function
    entirely and took both the plain-call retry and provider failover with
    it - the one case with two remedies got neither.
    """
    monkeypatch.setattr(config, "LLM_STREAMING", True)
    monkeypatch.setattr(config, "GROQ_MODEL", "model-a")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", False)

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "model-a":
            return _rate_limited()
        return _tool_response("open_app", {"app": "notepad"})

    async def go():
        client = _client(handler)
        brain = Brain(client)
        brain.provider = "groq"
        brain._groq_rotation = ["model-a", "model-b"]
        call = await brain.decide("open notepad", on_sentence=lambda s: None)
        await client.aclose()
        return call

    assert _run(go()).name == "open_app"


def test_the_rotation_never_includes_the_vision_model(monkeypatch):
    """Sharing a bucket with vision would undo the point of having two: one
    screen task is a dozen framed requests and would empty the brain's budget
    on the way past.
    """
    monkeypatch.setattr(config, "GROQ_MODEL", "brain-model")
    monkeypatch.setattr(config, "GROQ_MODEL_ROTATION", [])
    monkeypatch.setattr(
        config, "GROQ_MODEL_FALLBACKS", ["second-model", "vision-model"]
    )
    monkeypatch.setattr(config, "GROQ_VISION_MODEL", "vision-model")
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_ROTATION_AVOIDS_VISION", True)

    async def go():
        client = _client(lambda r: _tool_response("chat", {"reply": "hi"}))
        brain = Brain(client)
        brain._build_rotation({"brain-model", "second-model", "vision-model"})
        await client.aclose()
        return brain._groq_rotation

    assert _run(go()) == ["brain-model", "second-model"]


def test_the_rotation_only_lists_models_the_account_actually_has(monkeypatch):
    monkeypatch.setattr(config, "GROQ_MODEL", "brain-model")
    monkeypatch.setattr(config, "GROQ_MODEL_ROTATION", [])
    monkeypatch.setattr(config, "GROQ_MODEL_FALLBACKS", ["real", "retired"])
    monkeypatch.setattr(config, "VISION_PROVIDER", "gemini")

    async def go():
        client = _client(lambda r: _tool_response("chat", {"reply": "hi"}))
        brain = Brain(client)
        brain._build_rotation({"brain-model", "real"})
        await client.aclose()
        return brain._groq_rotation

    assert _run(go()) == ["brain-model", "real"]


def test_verify_model_fills_the_rotation(monkeypatch):
    monkeypatch.setattr(config, "GROQ_MODEL", "brain-model")
    monkeypatch.setattr(config, "GROQ_MODEL_ROTATION", [])
    monkeypatch.setattr(config, "GROQ_MODEL_FALLBACKS", ["spare"])
    monkeypatch.setattr(config, "VISION_PROVIDER", "gemini")

    async def go():
        client = _client(lambda r: _models("brain-model", "spare"))
        brain = Brain(client)
        brain.provider = "groq"
        resolved = await brain.verify_model()
        await client.aclose()
        return resolved, brain._groq_rotation

    resolved, rotation = _run(go())
    assert resolved == "brain-model"
    assert rotation == ["brain-model", "spare"]


def test_rotation_state_does_not_leak_between_sessions(monkeypatch):
    """`_groq_rotation` has a mutable class-level default so that a `Brain`
    built without `__init__` still has one. That is only safe while it is
    replaced rather than appended to.
    """
    monkeypatch.setattr(config, "GROQ_MODEL", "brain-model")
    monkeypatch.setattr(config, "GROQ_MODEL_ROTATION", [])
    monkeypatch.setattr(config, "GROQ_MODEL_FALLBACKS", ["spare"])
    monkeypatch.setattr(config, "VISION_PROVIDER", "gemini")

    async def go():
        client = _client(lambda r: _tool_response("chat", {"reply": "hi"}))
        first = Brain(client)
        first._build_rotation({"brain-model", "spare"})
        second = Brain(client)
        await client.aclose()
        return first._groq_rotation, second._groq_rotation, Brain._groq_rotation

    first, second, shared = _run(go())
    assert first == ["brain-model", "spare"]
    assert second == []
    assert shared == []


def test_a_listed_but_unusable_model_is_dropped_rather_than_surfaced(monkeypatch):
    """`groq/compound` is the case this exists for: the catalogue lists it and
    using it answers 403, because it is disabled in the project's own
    settings. It looks usable right up until a rate limit sends E.V. to it,
    and the user can do nothing with "the model sent back an error, 403".
    """
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    monkeypatch.setattr(config, "GROQ_MODEL", "model-a")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", False)

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "model-a":
            return _rate_limited()
        if model == "blocked":
            return httpx.Response(403, json={"error": {"message": "blocked"}})
        return _tool_response("open_app", {"app": "notepad"})

    async def go():
        client = _client(handler)
        brain = Brain(client)
        brain.provider = "groq"
        brain._groq_rotation = ["model-a", "blocked", "model-c"]
        call = await brain.decide("open notepad")
        await client.aclose()
        return call, brain

    call, brain = _run(go())
    assert call.name == "open_app"
    assert "blocked" not in brain._groq_rotation
    assert [json.loads(r.content)["model"] for r in REQUESTS] == [
        "model-a",
        "blocked",
        "model-c",
    ]


def test_the_last_remaining_model_being_unusable_is_the_user_s_problem(monkeypatch):
    """With nothing left to spend, a 403 is real configuration news and has to
    reach the user rather than being swallowed by a rotation of one.
    """
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    monkeypatch.setattr(config, "GROQ_MODEL", "only")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", False)

    async def go():
        client = _client(
            lambda r: httpx.Response(403, json={"error": {"message": "blocked"}})
        )
        brain = Brain(client)
        brain.provider = "groq"
        brain._groq_rotation = ["only"]
        with pytest.raises(BrainError) as excinfo:
            await brain.decide("open notepad")
        await client.aclose()
        return excinfo.value

    exc = _run(go())
    assert exc.model_unavailable is True
    assert "{" not in str(exc)  # spoken aloud


def test_every_bucket_is_tried_at_most_once_per_turn(monkeypatch):
    """The rotation wraps, so a miscounted loop spends a second request on a
    model that was rate limited moments ago - which is exactly the budget
    this is all trying to save.
    """
    monkeypatch.setattr(config, "LLM_STREAMING", False)
    monkeypatch.setattr(config, "GROQ_MODEL", "model-a")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAILOVER", False)

    async def go():
        client = _client(lambda r: _rate_limited())
        brain = Brain(client)
        brain.provider = "groq"
        brain._groq_rotation = ["model-a", "model-b", "model-c"]
        with pytest.raises(BrainError):
            await brain.decide("open notepad")
        await client.aclose()

    _run(go())
    models = [json.loads(r.content)["model"] for r in REQUESTS]
    assert models == ["model-a", "model-b", "model-c"]
    assert len(models) == len(set(models))


def test_the_configured_model_is_checked_for_availability_like_any_other(
    monkeypatch,
):
    """It is not automatically usable. `verify_model` may be about to fall
    back from it, and `--check` builds a rotation before any of that has
    happened - so seeding the list with it unconditionally put a model that
    404s at the front of the rotation.
    """
    monkeypatch.setattr(config, "GROQ_MODEL", "retired")
    monkeypatch.setattr(config, "GROQ_MODEL_ROTATION", [])
    monkeypatch.setattr(config, "GROQ_MODEL_FALLBACKS", ["real-a", "real-b"])
    monkeypatch.setattr(config, "VISION_PROVIDER", "gemini")

    async def go():
        client = _client(lambda r: _tool_response("chat", {"reply": "hi"}))
        brain = Brain(client)
        brain._build_rotation({"real-a", "real-b"})
        await client.aclose()
        return brain._groq_rotation

    assert _run(go()) == ["real-a", "real-b"]


def test_the_whole_vision_ladder_is_kept_out_of_the_rotation(monkeypatch):
    """Vision falls back exactly as the brain does, so the model it will
    actually land on is usually not the one named in the config. Skipping
    only the configured name left the brain rotating onto the very model
    vision was about to start using.
    """
    monkeypatch.setattr(config, "GROQ_MODEL", "brain-model")
    monkeypatch.setattr(config, "GROQ_MODEL_ROTATION", [])
    monkeypatch.setattr(config, "GROQ_MODEL_FALLBACKS", ["spare", "vision-spare"])
    monkeypatch.setattr(config, "GROQ_VISION_MODEL", "vision-retired")
    monkeypatch.setattr(config, "GROQ_VISION_FALLBACKS", ["vision-spare"])
    monkeypatch.setattr(config, "VISION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_ROTATION_AVOIDS_VISION", True)

    async def go():
        client = _client(lambda r: _tool_response("chat", {"reply": "hi"}))
        brain = Brain(client)
        brain._build_rotation({"brain-model", "spare", "vision-spare"})
        await client.aclose()
        return brain._groq_rotation

    assert _run(go()) == ["brain-model", "spare"]
