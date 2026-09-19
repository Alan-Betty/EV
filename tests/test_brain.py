"""Brain and event-loop tests against a mock transport - no network, no key.

Verifies the exact wire format each provider expects, and that the core loop
handles confirmations, tool failures and API errors without falling over.

Run with:  python tests/test_brain.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["GROQ_API_KEY"] = "test-groq-key"
os.environ["GEMINI_API_KEY"] = "test-gemini-key"
os.environ["EV_TTS_ENABLED"] = "false"
os.environ["EV_TEXT_MODE"] = "true"

import httpx  # noqa: E402

import config  # noqa: E402
from ev.brain import Brain, BrainError  # noqa: E402
from tools.schemas import TOOL_NAMES, TOOL_SPECS  # noqa: E402

# The environment variables above only reach `config` if this module is the
# first one to import it, which depends on the order pytest happens to collect
# files in. The wire-format assertions below check the exact key that is sent,
# so pin the values here too rather than relying on collection order.
config.GROQ_API_KEY = "test-groq-key"
config.GEMINI_API_KEY = "test-gemini-key"

CAPTURED: list[httpx.Request] = []


def _client(handler) -> httpx.AsyncClient:
    def _wrapped(request: httpx.Request) -> httpx.Response:
        CAPTURED.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(_wrapped))


def _groq_tool_response(name: str, arguments: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    # Groq returns arguments as a JSON *string*.
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )


def _gemini_tool_response(name: str, args: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}}
            ]
        },
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------


def test_groq_request_shape_and_tool_parsing():
    CAPTURED.clear()
    config.LLM_PROVIDER = "groq"

    async def go():
        client = _client(lambda r: _groq_tool_response("open_app", {"app": "chrome"}))
        brain = Brain(client)
        brain.provider = "groq"
        call = await brain.decide("open chrome")
        await client.aclose()
        return call

    call = _run(go())
    assert call.name == "open_app"
    assert call.arguments == {"app": "chrome"}

    request = CAPTURED[-1]
    assert request.url.path.endswith("/chat/completions")
    assert request.headers["authorization"] == "Bearer test-groq-key"
    body = json.loads(request.content)
    assert body["model"] == config.GROQ_MODEL
    assert body["tool_choice"] == "required"
    assert body["messages"][0]["role"] == "system"
    assert "E.V." in body["messages"][0]["content"]
    names = {tool["function"]["name"] for tool in body["tools"]}
    assert names == set(TOOL_NAMES)


def test_gemini_request_shape_and_function_call_parsing():
    CAPTURED.clear()
    config.LLM_PROVIDER = "gemini"
    try:

        async def go():
            client = _client(
                lambda r: _gemini_tool_response("web_search", {"query": "gaming mouse"})
            )
            brain = Brain(client)
            brain.provider = "gemini"
            call = await brain.decide("find me a gaming mouse")
            await client.aclose()
            return call

        call = _run(go())
        assert call.name == "web_search"
        assert call.arguments == {"query": "gaming mouse"}

        request = CAPTURED[-1]
        assert ":generateContent" in str(request.url)
        assert request.headers["x-goog-api-key"] == "test-gemini-key"
        body = json.loads(request.content)
        assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
        assert "systemInstruction" in body
        assert len(body["tools"][0]["functionDeclarations"]) == len(TOOL_SPECS)

    finally:
        config.LLM_PROVIDER = "groq"


def test_history_is_sent_and_stays_bounded():
    CAPTURED.clear()

    async def go():
        client = _client(lambda r: _groq_tool_response("chat", {"reply": "Sure."}))
        brain = Brain(client)
        brain.provider = "groq"
        for i in range(config.HISTORY_TURNS + 5):
            brain.remember(f"question {i}", f"answer {i}")
        await brain.decide("latest")
        await client.aclose()
        return brain

    brain = _run(go())
    assert len(brain.history) == config.HISTORY_TURNS

    body = json.loads(CAPTURED[-1].content)
    # system + 2 per remembered turn + the new user message
    assert len(body["messages"]) == 1 + config.HISTORY_TURNS * 2 + 1
    assert body["messages"][-1]["content"] == "latest"


def test_prose_instead_of_a_tool_call_degrades_to_chat():
    async def go():
        client = _client(
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": "Hello there."}}]}
            )
        )
        brain = Brain(client)
        brain.provider = "groq"
        call = await brain.decide("hi")
        await client.aclose()
        return call

    call = _run(go())
    assert call.name == "chat"
    assert call.arguments["reply"] == "Hello there."


def test_malformed_argument_json_does_not_crash():
    async def go():
        response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "open_app", "arguments": "{not json"}}
                            ]
                        }
                    }
                ]
            },
        )
        client = _client(lambda r: response)
        brain = Brain(client)
        brain.provider = "groq"
        call = await brain.decide("open something")
        await client.aclose()
        return call

    call = _run(go())
    assert call.name == "open_app"
    assert call.arguments == {}


def test_api_errors_become_readable_messages():
    for status, fragment in ((401, "key"), (429, "rate limit"), (500, "500")):

        async def go(status=status):
            client = _client(lambda r: httpx.Response(status, text="nope"))
            brain = Brain(client)
            brain.provider = "groq"
            try:
                await brain.decide("hi")
            except BrainError as exc:
                return str(exc)
            finally:
                await client.aclose()
            return ""

        message = _run(go())
        assert fragment.lower() in message.lower(), (status, message)


def test_missing_api_key_is_caught_at_construction():
    original = config.GROQ_API_KEY
    config.GROQ_API_KEY = ""
    try:
        Brain(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    except BrainError as exc:
        assert "GROQ_API_KEY" in str(exc)
    else:
        raise AssertionError("expected BrainError for a missing key")
    finally:
        config.GROQ_API_KEY = original


def test_core_loop_runs_a_tool_and_records_history():
    import ev_core

    async def go():
        client = _client(
            lambda r: _groq_tool_response("chat", {"reply": "Two plus two is four."})
        )
        assistant = ev_core.EV(text_mode=True)
        await assistant._http.aclose()
        assistant._http = client
        assistant.brain = Brain(client)
        assistant.brain.provider = "groq"
        assistant.transcriber._client = client

        await assistant.handle("what is two plus two")
        history = list(assistant.brain.history)
        await assistant.stop()
        return history

    history = _run(go())
    assert len(history) == 1
    assert "four" in history[0].assistant


def test_core_loop_holds_and_cancels_a_risky_command():
    import ev_core

    async def go():
        client = _client(
            lambda r: _groq_tool_response(
                "terminal_command", {"command": "del notes.txt"}
            )
        )
        assistant = ev_core.EV(text_mode=True)
        await assistant._http.aclose()
        assistant._http = client
        assistant.brain = Brain(client)
        assistant.brain.provider = "groq"

        spoken: list[str] = []
        assistant.say = lambda text: spoken.append(text) or asyncio.sleep(0)

        await assistant.handle("delete my notes file")
        held = assistant.session.pending is not None
        # In text mode the confirmation arrives as the next command.
        await assistant.handle("no, forget it")
        cleared = assistant.session.pending is None
        await assistant.stop()
        return held, cleared, spoken

    held, cleared, spoken = _run(go())
    assert held, "risky command should have been held for confirmation"
    assert cleared, "pending command should be cleared after the reply"
    assert any("Confirm" in line for line in spoken), spoken
    assert any("Cancelled" in line for line in spoken), spoken


def test_core_loop_executes_after_an_affirmative():
    import ev_core
    import tools

    async def go():
        client = _client(
            lambda r: _groq_tool_response(
                "terminal_command", {"command": "git reset --hard"}
            )
        )
        assistant = ev_core.EV(text_mode=True)
        await assistant._http.aclose()
        assistant._http = client
        assistant.brain = Brain(client)
        assistant.brain.provider = "groq"
        assistant.say = lambda text: asyncio.sleep(0)

        ran: list[dict] = []
        original = tools.dispatch

        # `cancel` is the cooperative stop token the core loop now threads
        # through every dispatch; it is passed positionally.
        def spy(name, arguments=None, cancel=None):
            if name == "terminal_command" and (arguments or {}).get("confirmed"):
                ran.append(arguments)
                return tools.ToolResult.success("Done.")
            return original(name, arguments, cancel)

        ev_core.dispatch = spy
        try:
            await assistant.handle("reset the repo")
            await assistant.handle("yes")
        finally:
            ev_core.dispatch = original
            await assistant.stop()
        return ran

    ran = _run(go())
    assert len(ran) == 1
    assert ran[0]["command"] == "git reset --hard"
    assert ran[0]["confirmed"] is True
    # `reason` is internal bookkeeping and must not reach the tool.
    assert "reason" not in ran[0]


def _main() -> int:
    tests = [
        (name, function)
        for name, function in sorted(globals().items())
        if name.startswith("test_") and callable(function)
    ]
    failures = 0
    for name, function in tests:
        try:
            function()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}\n     {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            failures_text = traceback.format_exc().strip().splitlines()[-1]
            print(f"ERROR {name}\n      {failures_text}")
        else:
            print(f"pass {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())


# ---------------------------------------------------------------------------
# A reply that is really a tool call
# ---------------------------------------------------------------------------
# The last rung of the tool_use_failed ladder asks for prose and sometimes
# gets JSON: the model knew which tool it wanted and wrote it in the content
# field. "Open Gmail and give me a summary of the important mail" produced a
# flawless browser_task object that way, and it was read out, braces and all.
def test_a_tool_call_written_as_prose_is_rescued():
    from ev.brain import _salvage_tool_call

    call = _salvage_tool_call(
        '{"name": "browser_task", "arguments": {"task": "Open Gmail"}}'
    )
    assert call is not None
    assert call.name == "browser_task"
    assert call.arguments["task"] == "Open Gmail"


def test_a_rescued_call_survives_a_code_fence():
    from ev.brain import _salvage_tool_call

    call = _salvage_tool_call(
        '```json\n{"name": "screen_task", "arguments": {"task": "open notepad"}}\n```'
    )
    assert call is not None and call.name == "screen_task"


def test_the_nested_function_shape_is_rescued_too():
    from ev.brain import _salvage_tool_call

    call = _salvage_tool_call(
        '{"function": {"name": "open_app", "arguments": {"app": "chrome"}}}'
    )
    assert call is not None and call.name == "open_app"


def test_ordinary_speech_is_never_mistaken_for_a_tool_call():
    """The strictness is the point: a chat answer that quotes some JSON must
    be spoken, not executed."""
    from ev.brain import _salvage_tool_call

    assert _salvage_tool_call("Three thirteen point two. Modern of you.") is None
    assert _salvage_tool_call("") is None
    assert _salvage_tool_call('{"name": "no_such_tool", "arguments": {}}') is None
    assert _salvage_tool_call("Use {\"name\": \"open_app\"} for that.") is None


def test_a_rescued_chat_call_stays_prose():
    """Rescuing a `chat` would be a no-op that threw away the actual words."""
    from ev.brain import _salvage_tool_call

    assert _salvage_tool_call('{"name": "chat", "arguments": {"reply": "hi"}}') is None
