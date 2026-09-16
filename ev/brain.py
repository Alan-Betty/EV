"""The brain: Groq or Gemini, called over raw HTTP, returning tool calls.

Deliberately no vendor SDK. `httpx` is already needed for the STT upload, and
both providers are a single JSON POST, so skipping the SDKs saves roughly
40 MB of resident memory and a pile of transitive dependencies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

import config
from tools.schemas import to_gemini_tools, to_openai_tools

log = logging.getLogger("ev.brain")


class BrainError(RuntimeError):
    """The model could not be reached or answered with something unusable."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn:
    """One exchange, kept in the rolling history."""

    user: str
    assistant: str


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
    def remember(self, user: str, assistant: str) -> None:
        self.history.append(Turn(user, assistant))
        if len(self.history) > config.HISTORY_TURNS:
            del self.history[: -config.HISTORY_TURNS]

    def forget(self) -> None:
        self.history.clear()

    # -- inference --------------------------------------------------------
    async def decide(self, transcript: str, extra_context: str = "") -> ToolCall:
        """Pick a tool for one user utterance.

        Falls back to a `chat` call rather than raising, because a voice
        assistant that goes silent on a malformed response is worse than one
        that admits it is confused.
        """
        if self.provider == "gemini":
            return await self._decide_gemini(transcript, extra_context)
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

        if response.status_code == 401:
            raise BrainError("API key rejected. Check the key in your .env file.")
        if response.status_code == 429:
            raise BrainError("Rate limited. Give it a few seconds.")
        if response.status_code >= 400:
            raise BrainError(f"Model returned {response.status_code}: {response.text[:200]}")

        try:
            return response.json()
        except ValueError as exc:
            raise BrainError("The model sent back something that was not JSON.") from exc

    # -- Groq (OpenAI-compatible) ----------------------------------------
    async def _decide_groq(self, transcript: str, extra_context: str) -> ToolCall:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(extra_context)}
        ]
        for turn in self.history:
            messages.append({"role": "user", "content": turn.user})
            messages.append({"role": "assistant", "content": turn.assistant})
        messages.append({"role": "user", "content": transcript})

        payload = {
            "model": config.GROQ_MODEL,
            "messages": messages,
            "tools": to_openai_tools(),
            "tool_choice": "required",
            "temperature": config.LLM_TEMPERATURE,
            "max_tokens": config.LLM_MAX_TOKENS,
        }
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

    # -- Gemini -----------------------------------------------------------
    async def _decide_gemini(self, transcript: str, extra_context: str) -> ToolCall:
        contents: list[dict[str, Any]] = []
        for turn in self.history:
            contents.append({"role": "user", "parts": [{"text": turn.user}]})
            contents.append({"role": "model", "parts": [{"text": turn.assistant}]})
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
        url = (
            f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_MODEL}:generateContent"
        )
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
