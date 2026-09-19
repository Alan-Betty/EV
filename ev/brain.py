"""The brain: Groq or Gemini, called over raw HTTP, returning tool calls.

Deliberately no vendor SDK. `httpx` is already needed for the STT upload, and
both providers are a single JSON POST, so skipping the SDKs saves roughly
40 MB of resident memory and a pile of transitive dependencies.

Two rules here exist because breaking either one is audible:

* **The assistant channel carries speech and nothing else.** Tool observations
  ("exit=0", "Launched chrome.exe") go in a separate field and are replayed in
  an input role. Storing them as assistant turns teaches the model, by
  example, to prefix its own replies with labels - which is exactly how E.V.
  ended up saying "Spoke:" out loud.
* **Speech starts before the model finishes.** With streaming on, the `chat`
  reply is pulled out of the partial tool-call JSON and handed to the speaker
  a sentence at a time, so the first word plays while the rest is still
  being generated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

import config
from tools.schemas import TOOL_NAMES, to_gemini_tools, to_openai_tools

log = logging.getLogger("ev.brain")

# Called with each complete sentence of a streamed `chat` reply.
SentenceHook = Callable[[str], None]


class BrainError(RuntimeError):
    """The model could not be reached or answered with something unusable.

    `tool_failure` marks one specific, recoverable case: the API accepted the
    request but rejected the model's own tool call - either it emitted no call
    at all under `tool_choice: required`, or it emitted arguments that were
    not valid JSON. Groq returns both as a 400 with code `tool_use_failed`.

    That distinction is load-bearing. A rejected tool call is not a broken
    key or a dead network; the same request usually succeeds a second time
    with the constraint relaxed, and failing it outright is what made a
    perfectly ordinary request come back with nothing a user could act on.
    """

    def __init__(self, message: str, tool_failure: bool = False) -> None:
        super().__init__(message)
        self.tool_failure = tool_failure


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn:
    """One exchange, kept in the rolling history.

    `assistant` is only ever what E.V. said out loud. `observation` is what the
    tool actually did, replayed to the model as input rather than as its own
    words - see the module docstring.
    """

    user: str
    assistant: str
    observation: str = ""


_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)m(?!s))?\s*(?:(\d+(?:\.\d+)?)s)?")


def _retry_after(headers: Any) -> float | None:
    """How long the server says to wait, in seconds, or None.

    Groq answers in two shapes: a plain `retry-after` in seconds, and
    `x-ratelimit-reset-tokens` as a duration like "2m52.8s" or "547ms". Both
    are worth reading - guessing a backoff here would either give up while
    the window was about to open or sit on a microphone for a minute.
    """
    plain = headers.get("retry-after")
    if plain:
        try:
            return max(0.0, float(plain))
        except ValueError:
            pass

    raw = (headers.get("x-ratelimit-reset-tokens") or "").strip().lower()
    if not raw:
        return None
    if raw.endswith("ms"):
        try:
            return max(0.0, float(raw[:-2]) / 1000.0)
        except ValueError:
            return None
    match = _DURATION.fullmatch(raw)
    if not match or not any(match.groups()):
        return None
    minutes = float(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    return minutes * 60.0 + seconds


def _salvage_tool_call(text: str) -> ToolCall | None:
    """A prose reply that is really a tool call, rescued. None if it is prose.

    The last rung of the ladder asks for words and sometimes gets JSON: the
    model knew exactly which tool it wanted and simply wrote it in the
    content field instead of the tool-call field. "Open Gmail and give me a
    summary of the important mail" produced a flawless `browser_task` object
    that way.

    Speaking that is the worst of the three possible outcomes - worse than
    the tool running, and worse than an honest "I couldn't do that", because
    a speech synthesiser reads braces and quotation marks out loud. The
    intent is right there in the reply, so take it.

    Deliberately strict. The whole reply has to be one JSON object naming a
    tool that exists, which is what keeps an ordinary chat answer that
    happens to quote some JSON from being executed instead of spoken.
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return None

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name") or parsed.get("tool") or parsed.get("function")
    if isinstance(name, dict):  # {"function": {"name": ..., "arguments": ...}}
        parsed = name
        name = parsed.get("name")
    if not isinstance(name, str) or name not in TOOL_NAMES or name == "chat":
        return None

    arguments = parsed.get("arguments")
    if arguments is None:
        arguments = parsed.get("parameters")
    log.info("Rescued a %s call the model wrote as prose", name)
    return ToolCall(name, _coerce_arguments(arguments))


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a dict, a JSON string, or occasional garbage."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Tool arguments were not valid JSON: %.200s", text)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# -- streaming helpers -------------------------------------------------------

_REPLY_KEY = re.compile(r'"reply"\s*:\s*"')
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "\\": "\\",
    "/": "/",
}


def partial_reply(buffer: str) -> str:
    """Decode the `reply` string out of a half-written tool-call JSON blob.

    `json.loads` is useless mid-stream because the object has no closing brace
    yet, so this walks the value by hand and stops wherever the buffer runs
    out. An escape sequence split across two chunks is left for the next chunk
    rather than being decoded wrongly.
    """
    match = _REPLY_KEY.search(buffer)
    if not match:
        return ""

    out: list[str] = []
    index = match.end()
    while index < len(buffer):
        char = buffer[index]
        if char == "\\":
            if index + 1 >= len(buffer):
                break  # escape split across chunks; wait for more
            nxt = buffer[index + 1]
            if nxt == "u":
                if index + 6 > len(buffer):
                    break
                try:
                    out.append(chr(int(buffer[index + 2 : index + 6], 16)))
                except ValueError:
                    pass
                index += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            index += 2
            continue
        if char == '"':
            break  # closing quote: the value is complete
        out.append(char)
        index += 1
    return "".join(out)


class _SentenceEmitter:
    """Feeds complete sentences to a hook as a streamed reply grows.

    Holds back anything shorter than `TTS_STREAM_MIN_CHARS`, because "E.V."
    looks exactly like a finished sentence and synthesising four characters
    costs a full round trip for a fragment nobody wants to hear on its own.
    """

    def __init__(self, hook: SentenceHook | None) -> None:
        self._hook = hook
        self._emitted = 0

    def feed(self, text: str) -> None:
        if self._hook is None or len(text) <= self._emitted:
            return
        remainder = text[self._emitted :]
        breaks = list(_SENTENCE_BREAK.finditer(remainder))
        if not breaks:
            return
        chunk = remainder[: breaks[-1].start()].strip()
        if len(chunk) < config.TTS_STREAM_MIN_CHARS:
            return
        self._emitted += breaks[-1].end()
        self._hook(chunk)

    def flush(self, text: str) -> None:
        """Emit whatever is left once generation has finished."""
        if self._hook is None:
            return
        tail = text[self._emitted :].strip()
        if tail:
            self._emitted = len(text)
            self._hook(tail)


class Brain:
    """Turns a transcript into a tool call.

    One `httpx.AsyncClient` is shared for the process lifetime so connections
    stay warm; re-handshaking TLS on every utterance would add noticeable
    latency to a voice loop.
    """

    # Standing facts for this run: how long the user was away, what is still
    # on the backlog, what they have asked E.V. to remember. Set once at
    # startup by the core loop, unlike `extra_context`, which describes the
    # last action. Kept short - it costs tokens on every single turn.
    #
    # A class attribute rather than an instance one so that a `Brain` built
    # without `__init__` still has it.
    session_context: str = ""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.provider = config.LLM_PROVIDER
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S)
        self.history: list[Turn] = []

        if self.provider == "groq" and not config.GROQ_API_KEY:
            raise BrainError("GROQ_API_KEY is not set. Copy .env.example to .env.")
        if self.provider == "gemini" and not config.GEMINI_API_KEY:
            raise BrainError("GEMINI_API_KEY is not set. Copy .env.example to .env.")
        if self.provider not in {"groq", "gemini"}:
            raise BrainError(f"Unknown EV_LLM_PROVIDER '{self.provider}'. Use groq or gemini.")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- model availability ----------------------------------------------
    async def verify_model(self) -> str:
        """Confirm the configured model exists, or fall back to one that does.

        Groq's catalogue differs per account and changes over time, so a model
        name that is valid in the docs can still 404 here. Left unchecked that
        surfaces as E.V. answering every single command with an error, which
        reads like the API key is wrong. One cheap request at startup turns
        that into a one-line notice and a working assistant.
        """
        if self.provider != "groq":
            return config.GEMINI_MODEL

        try:
            response = await self._client.get(
                f"{config.GROQ_BASE_URL}/models",
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                timeout=10.0,
            )
            if response.status_code != 200:
                return config.GROQ_MODEL  # cannot tell; let the real call decide
            available = {item["id"] for item in response.json().get("data", [])}
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.debug("Could not list models: %s", exc)
            return config.GROQ_MODEL

        if config.GROQ_MODEL in available:
            return config.GROQ_MODEL

        for candidate in config.GROQ_MODEL_FALLBACKS:
            if candidate in available:
                log.warning(
                    "Model %r unavailable on this account; using %r",
                    config.GROQ_MODEL,
                    candidate,
                )
                print(
                    f"[E.V.] '{config.GROQ_MODEL}' isn't available on your Groq "
                    f"account. Using '{candidate}' instead."
                )
                config.GROQ_MODEL = candidate
                return candidate

        usable = sorted(
            name
            for name in available
            if not any(skip in name for skip in ("whisper", "guard", "orpheus", "tts"))
        )
        raise BrainError(
            f"Model '{config.GROQ_MODEL}' is not available on this account. "
            f"Chat models you do have: {', '.join(usable) or 'none'}. "
            "Set EV_GROQ_MODEL in your .env to one of them."
        )

    # -- history ----------------------------------------------------------
    def remember(self, user: str, assistant: str, observation: str = "") -> None:
        """Record one exchange.

        `assistant` must be the spoken reply only. Anything machine-flavoured
        belongs in `observation`, which never enters the assistant role.
        """
        self.history.append(
            Turn(user, assistant, (observation or "")[: config.HISTORY_OBSERVATION_CHARS])
        )
        if len(self.history) > config.HISTORY_TURNS:
            del self.history[: -config.HISTORY_TURNS]

    def forget(self) -> None:
        self.history.clear()

    # -- inference --------------------------------------------------------
    async def decide(
        self,
        transcript: str,
        extra_context: str = "",
        on_sentence: SentenceHook | None = None,
    ) -> ToolCall:
        """Pick a tool for one user utterance.

        `on_sentence`, when given alongside a streaming-capable provider, is
        called with each complete sentence of a `chat` reply as it arrives, so
        playback can begin before generation ends.

        Falls back to a `chat` call rather than raising, because a voice
        assistant that goes silent on a malformed response is worse than one
        that admits it is confused.
        """
        if self.provider == "gemini":
            return await self._decide_gemini(transcript, extra_context)

        if config.LLM_STREAMING and on_sentence is not None:
            try:
                return await self._decide_groq_streamed(
                    transcript, extra_context, on_sentence
                )
            except BrainError as exc:
                if not exc.tool_failure:
                    raise
                # Recoverable: the plain call retries with the constraint
                # relaxed and then, if needed, without tools at all.
                log.info("Streamed tool call rejected; retrying without streaming")
            except Exception as exc:
                # Streaming is an optimisation, never a dependency. A malformed
                # SSE frame must not cost the user their answer.
                log.warning("Streaming failed (%s); falling back to a plain call", exc)

        return await self._decide_groq(transcript, extra_context)

    def _system_prompt(self, extra_context: str) -> str:
        prompt = config.SYSTEM_PROMPT
        if self.session_context:
            prompt = f"{prompt}\n\nWHAT YOU ALREADY KNOW\n{self.session_context}"
        if extra_context:
            prompt = f"{prompt}\n\nCONTEXT FROM THE LAST ACTION\n{extra_context}"
        return prompt

    async def _post(self, url: str, payload: dict, headers: dict) -> dict:
        attempts = max(0, config.LLM_RATE_LIMIT_RETRIES) + 1
        for attempt in range(attempts):
            try:
                response = await self._client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise BrainError("The model timed out.") from exc
            except httpx.HTTPError as exc:
                raise BrainError(f"Network error reaching the model: {exc}") from exc

            if response.status_code != 429 or attempt == attempts - 1:
                break

            wait = _retry_after(response.headers)
            if wait is None or wait > config.LLM_RATE_LIMIT_MAX_WAIT_S:
                # Longer than anyone will stand at a microphone waiting.
                break
            log.info("Rate limited; waiting %.1fs before one retry", wait)
            await asyncio.sleep(wait)

        self._raise_for_status(response.status_code, response.text)

        try:
            return response.json()
        except ValueError as exc:
            raise BrainError("The model sent back something that was not JSON.") from exc

    @staticmethod
    def _raise_for_status(status: int, body: str) -> None:
        if status == 401:
            raise BrainError("API key rejected. Check the key in your .env file.")
        if status == 429:
            # Only reached once the wait in `_post` has already been tried,
            # so this really is "still busy" rather than "busy right now".
            raise BrainError("I'm over my rate limit. Try that again shortly.")
        if status >= 400:
            if "tool_use_failed" in body:
                # The model's own tool call was rejected, not the request.
                # Flagged so the caller can retry instead of surfacing it, and
                # phrased as English because this string is spoken aloud if
                # every retry fails - a raw JSON error body read out by a
                # speech synthesiser is the worst possible answer.
                raise BrainError(
                    "That one tangled me up. Try it a simpler way?",
                    tool_failure=True,
                )
            raise BrainError(f"Model returned {status}: {body[:200]}")

    # -- Groq (OpenAI-compatible) ----------------------------------------
    def _groq_messages(self, transcript: str, extra_context: str) -> list[dict[str, Any]]:
        """Build the message list, keeping the assistant role speech-only."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(extra_context)}
        ]
        for turn in self.history:
            messages.append({"role": "user", "content": turn.user})
            messages.append({"role": "assistant", "content": turn.assistant})
            if turn.observation:
                # An input-role note, so the model reads it as something that
                # happened rather than as a template for its own next reply.
                messages.append(
                    {
                        "role": "system",
                        "content": f"Result of that action: {turn.observation}",
                    }
                )
        messages.append({"role": "user", "content": transcript})
        return messages

    def _groq_payload(self, transcript: str, extra_context: str) -> dict[str, Any]:
        return {
            "model": config.GROQ_MODEL,
            "messages": self._groq_messages(transcript, extra_context),
            "tools": to_openai_tools(),
            "tool_choice": "required",
            "temperature": config.LLM_TEMPERATURE,
            "max_tokens": config.LLM_MAX_TOKENS,
        }

    async def _decide_groq(self, transcript: str, extra_context: str) -> ToolCall:
        payload = self._groq_payload(transcript, extra_context)
        headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}

        try:
            data = await self._post(
                f"{config.GROQ_BASE_URL}/chat/completions", payload, headers
            )
        except BrainError as exc:
            # Groq rejects the whole request when the model's tool call is
            # unusable - no call at all under `tool_choice: required`, or
            # arguments that were not valid JSON. Both are the model
            # stumbling, not the request being wrong, so neither is worth
            # handing back to the user.
            #
            # Two rungs down. First let the model choose whether to call a
            # tool; that alone fixes most of it. If even that comes back
            # broken, drop the tools entirely and ask for prose, which becomes
            # a spoken answer. A request like "open notepad and type the
            # second largest word in the dictionary" reliably broke the tool
            # call and, before this, ended the turn with nothing said at all.
            if not exc.tool_failure:
                raise
            log.info("Model's tool call was rejected; retrying with tool_choice=auto")
            payload["tool_choice"] = "auto"
            try:
                data = await self._post(
                    f"{config.GROQ_BASE_URL}/chat/completions", payload, headers
                )
            except BrainError as retry_exc:
                if not retry_exc.tool_failure:
                    raise
                log.info("Tool call rejected twice; falling back to a prose answer")
                return await self._plain_reply(transcript, extra_context)

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise BrainError(f"Unexpected Groq response shape: {str(data)[:200]}") from exc

        calls = message.get("tool_calls") or []
        if calls:
            function = calls[0].get("function", {})
            name = function.get("name", "")
            if name:
                return ToolCall(name, _coerce_arguments(function.get("arguments")))

        # `tool_choice: required` should prevent this, but models improvise.
        text = (message.get("content") or "").strip()
        rescued = _salvage_tool_call(text)
        if rescued is not None:
            return rescued
        log.info("Groq answered with prose instead of a tool call")
        return ToolCall("chat", {"reply": text or "I didn't catch that."})

    async def _plain_reply(self, transcript: str, extra_context: str) -> ToolCall:
        """Answer in prose, with no tools offered at all.

        The last rung of the ladder. Some requests reliably break the model's
        tool-calling - usually because they ask a question and an action in
        one breath - and when that happens twice, the useful thing left is the
        answer to the question. Stripping the tools removes the thing that was
        failing, so this call either produces words to say or a plain error,
        never another unusable tool call.
        """
        payload = self._groq_payload(transcript, extra_context)
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
        data = await self._post(
            f"{config.GROQ_BASE_URL}/chat/completions", payload, headers
        )
        try:
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError):
            text = ""
        rescued = _salvage_tool_call(text)
        if rescued is not None:
            return rescued
        return ToolCall(
            "chat",
            {"reply": text or "I couldn't work out how to do that one."},
        )

    async def _decide_groq_streamed(
        self, transcript: str, extra_context: str, on_sentence: SentenceHook
    ) -> ToolCall:
        """Stream the completion, speaking each sentence of a `chat` reply.

        Only `chat` may start early: every other tool has a side effect, and
        announcing "Chrome's up" before Chrome is up would be a lie. For those,
        the stream is still consumed - it just yields the tool call at the end,
        exactly as the non-streaming path does.
        """
        payload = {**self._groq_payload(transcript, extra_context), "stream": True}
        headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}

        # Keyed by the index the API assigns each call. A model answering
        # "open notepad and type X" emits two calls in one completion, and
        # concatenating their argument fragments into one buffer produced
        # `{"name": "notepad"}{"action": "create"}` - not valid JSON, so every
        # argument was dropped and the tool ran on nothing. Only the first
        # call is acted on, matching the non-streaming path.
        calls: dict[int, dict[str, str]] = {}
        prose: list[str] = []
        emitter = _SentenceEmitter(on_sentence)

        try:
            async with self._client.stream(
                "POST",
                f"{config.GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    self._raise_for_status(response.status_code, body)

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        event = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue

                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    if delta.get("content"):
                        prose.append(delta["content"])

                    for call in delta.get("tool_calls") or []:
                        function = call.get("function") or {}
                        try:
                            index = int(call.get("index", 0))
                        except (TypeError, ValueError):
                            index = 0
                        slot = calls.setdefault(index, {"name": "", "arguments": ""})
                        if function.get("name"):
                            slot["name"] = function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]

                    # Speaking starts here, mid-generation. This is the whole
                    # point of the streaming path.
                    first = calls.get(min(calls)) if calls else None
                    if first and first["name"] == "chat" and first["arguments"]:
                        emitter.feed(partial_reply(first["arguments"]))
        except httpx.TimeoutException as exc:
            raise BrainError("The model timed out.") from exc
        except httpx.HTTPError as exc:
            raise BrainError(f"Network error reaching the model: {exc}") from exc

        if len(calls) > 1:
            log.info(
                "Model asked for %d tools in one turn; acting on the first",
                len(calls),
            )

        first = calls.get(min(calls)) if calls else None
        if first and first["name"]:
            name, arguments = first["name"], first["arguments"]
            parsed = _coerce_arguments(arguments)
            if name == "chat":
                reply = str(parsed.get("reply", "") or partial_reply(arguments)).strip()
                emitter.flush(reply)
                return ToolCall("chat", {"reply": reply or "I didn't catch that."})
            return ToolCall(name, parsed)

        text = "".join(prose).strip()
        if text:
            log.info("Groq streamed prose instead of a tool call")
            emitter.flush(text)
            return ToolCall("chat", {"reply": text})

        # Neither a tool call nor a word of prose. Groq does this when the
        # model's own tool call was unusable: the stream simply ends empty,
        # with no error frame to catch. Answering "I didn't catch that" here
        # blamed the user for the model's stumble, so the plain call - which
        # retries and then falls back to prose - gets its turn instead.
        log.info("Streamed completion was empty; falling back to a plain call")
        return await self._decide_groq(transcript, extra_context)

    # -- Gemini -----------------------------------------------------------
    async def _decide_gemini(self, transcript: str, extra_context: str) -> ToolCall:
        contents: list[dict[str, Any]] = []
        for turn in self.history:
            contents.append({"role": "user", "parts": [{"text": turn.user}]})
            contents.append({"role": "model", "parts": [{"text": turn.assistant}]})
            if turn.observation:
                # Gemini has no mid-conversation system role, so observations
                # ride in the user channel. What matters is only that they stay
                # out of the model role, where they would be read as a pattern
                # for E.V.'s own replies.
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": f"Result of that action: {turn.observation}"}],
                    }
                )
        contents.append({"role": "user", "parts": [{"text": transcript}]})

        payload = {
            "systemInstruction": {"parts": [{"text": self._system_prompt(extra_context)}]},
            "contents": contents,
            "tools": to_gemini_tools(),
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {
                "temperature": config.LLM_TEMPERATURE,
                "maxOutputTokens": config.LLM_MAX_TOKENS,
            },
        }
        url = f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_MODEL}:generateContent"
        headers = {"x-goog-api-key": config.GEMINI_API_KEY}

        data = await self._post(url, payload, headers)

        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as exc:
            blocked = data.get("promptFeedback", {}).get("blockReason")
            if blocked:
                raise BrainError(f"Gemini blocked that request: {blocked}") from exc
            raise BrainError(f"Unexpected Gemini response shape: {str(data)[:200]}") from exc

        for part in parts:
            call = part.get("functionCall")
            if call and call.get("name"):
                return ToolCall(call["name"], _coerce_arguments(call.get("args")))

        text = " ".join(part.get("text", "") for part in parts).strip()
        log.info("Gemini answered with prose instead of a function call")
        return ToolCall("chat", {"reply": text or "I didn't catch that."})
