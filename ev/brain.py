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

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

import config
from tools.schemas import to_gemini_tools, to_openai_tools

log = logging.getLogger("ev.brain")

# Called with each complete sentence of a streamed `chat` reply.
SentenceHook = Callable[[str], None]


class BrainError(RuntimeError):
    """The model could not be reached or answered with something unusable."""


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
            except BrainError:
                raise
            except Exception as exc:
                # Streaming is an optimisation, never a dependency. A malformed
                # SSE frame must not cost the user their answer.
                log.warning("Streaming failed (%s); falling back to a plain call", exc)

        return await self._decide_groq(transcript, extra_context)

    def _system_prompt(self, extra_context: str) -> str:
        if not extra_context:
            return config.SYSTEM_PROMPT
        return f"{config.SYSTEM_PROMPT}\n\nCONTEXT FROM THE LAST ACTION\n{extra_context}"

    async def _post(self, url: str, payload: dict, headers: dict) -> dict:
        try:
            response = await self._client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise BrainError("The model timed out.") from exc
        except httpx.HTTPError as exc:
            raise BrainError(f"Network error reaching the model: {exc}") from exc

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
            raise BrainError("Rate limited. Give it a few seconds.")
        if status >= 400:
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
            # Smaller models cannot always honour tool_choice=required and the
            # API rejects the whole request rather than degrading. Retry once
            # letting the model choose; a prose answer becomes a chat reply.
            if "did not call a tool" not in str(exc):
                raise
            log.info("Model would not force a tool call; retrying with tool_choice=auto")
            payload["tool_choice"] = "auto"
            data = await self._post(
                f"{config.GROQ_BASE_URL}/chat/completions", payload, headers
            )

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
        log.info("Groq answered with prose instead of a tool call")
        return ToolCall("chat", {"reply": text or "I didn't catch that."})

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

        name = ""
        arguments = ""
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
                        if function.get("name"):
                            name = function["name"]
                        if function.get("arguments"):
                            arguments += function["arguments"]

                    # Speaking starts here, mid-generation. This is the whole
                    # point of the streaming path.
                    if name == "chat" and arguments:
                        emitter.feed(partial_reply(arguments))
        except httpx.TimeoutException as exc:
            raise BrainError("The model timed out.") from exc
        except httpx.HTTPError as exc:
            raise BrainError(f"Network error reaching the model: {exc}") from exc

        if name:
            parsed = _coerce_arguments(arguments)
            if name == "chat":
                reply = str(parsed.get("reply", "") or partial_reply(arguments)).strip()
                emitter.flush(reply)
                return ToolCall("chat", {"reply": reply or "I didn't catch that."})
            return ToolCall(name, parsed)

        text = "".join(prose).strip()
        log.info("Groq streamed prose instead of a tool call")
        emitter.flush(text)
        return ToolCall("chat", {"reply": text or "I didn't catch that."})

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
