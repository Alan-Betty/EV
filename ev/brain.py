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
from tools.schemas import (
    TOOL_NAMES,
    select_tools,
    to_gemini_tools,
    to_openai_tools,
)

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

    `rate_limited` marks the other recoverable case: the provider is over
    quota right now. It is separate from `tool_failure` because the remedy is
    different - a rejected tool call is retried against the same provider with
    the constraint relaxed, while a rate limit is only fixed by waiting or by
    asking somebody else.
    """

    def __init__(
        self,
        message: str,
        tool_failure: bool = False,
        rate_limited: bool = False,
        model_unavailable: bool = False,
    ) -> None:
        super().__init__(message)
        self.tool_failure = tool_failure
        self.rate_limited = rate_limited
        # 403 or 404 on a model name: not an account-wide problem and not
        # worth the user's attention, because the rotation has other buckets
        # to spend. `groq/compound` is the case that made this necessary - it
        # is listed by /models and 403s on use, so it looks usable right up
        # until a rate limit sends E.V. to it.
        self.model_unavailable = model_unavailable


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


def _gemini_retry_after(body: str) -> float | None:
    """How long Gemini says to wait, in seconds, or None.

    Gemini does not answer a 429 with `retry-after`. It answers with a JSON
    body carrying a `RetryInfo` detail whose `retryDelay` reads like "31s".
    Read from the headers alone, every Gemini rate limit looked like one with
    no stated delay - so E.V. never waited, and gave up on a window that was
    usually about to open.
    """
    try:
        parsed = json.loads(body or "")
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    details = parsed.get("error", {})
    if not isinstance(details, dict):
        return None
    for detail in details.get("details") or ():
        if not isinstance(detail, dict):
            continue
        raw = str(detail.get("retryDelay") or "").strip().lower()
        if not raw:
            continue
        if raw.endswith("ms"):
            try:
                return max(0.0, float(raw[:-2]) / 1000.0)
            except ValueError:
                continue
        match = _DURATION.fullmatch(raw.rstrip())
        if match and any(match.groups()):
            return float(match.group(1) or 0) * 60.0 + float(match.group(2) or 0)
    return None


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

    # Class attributes for the same reason, and they carry one rule: both are
    # only ever *replaced*, never mutated in place, so the shared default
    # list cannot pick up one session's models and hand them to the next.
    _groq_model: str = ""
    _groq_rotation: list[str] = []

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.provider = config.LLM_PROVIDER
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S)
        self.history: list[Turn] = []

        # Set while a request is being served by the *other* provider after a
        # rate limit. Not a mode: it lasts one call and is cleared in a
        # `finally`, so a single 429 never silently relocates the session.
        self._failing_over = False

        # Which Groq model this session is currently spending. Empty means
        # "whatever `config.GROQ_MODEL` says"; a rate limit moves it along
        # `_groq_rotation` and it stays moved, because the bucket it just
        # left needs a minute to refill. `_groq_rotation` is filled in by
        # `verify_model`, which is the only place that knows which models
        # this account actually has.
        self._groq_model = ""
        self._groq_rotation = []

        if self.provider not in {"groq", "gemini"}:
            raise BrainError(f"Unknown EV_LLM_PROVIDER '{self.provider}'. Use groq or gemini.")
        if self.provider == "groq" and not config.GROQ_API_KEY:
            raise BrainError("GROQ_API_KEY is not set. Copy .env.example to .env.")
        if self.provider == "gemini" and not config.GEMINI_API_KEY:
            raise BrainError("GEMINI_API_KEY is not set. Copy .env.example to .env.")

    # -- provider failover -------------------------------------------------
    @staticmethod
    def _other_provider(provider: str) -> str:
        return "gemini" if provider == "groq" else "groq"

    def _failover_target(self) -> str:
        """The provider to try when this one is rate limited, or "".

        Read at call time rather than cached, so a key added to `.env` and a
        reload mid-session are enough to enable it.
        """
        if not config.LLM_PROVIDER_FAILOVER or self._failing_over:
            return ""
        other = self._other_provider(self.provider)
        key = config.GROQ_API_KEY if other == "groq" else config.GEMINI_API_KEY
        return other if key else ""

    # -- token buckets -----------------------------------------------------
    @property
    def groq_model(self) -> str:
        """The Groq model to spend on the next request."""
        return self._groq_model or config.GROQ_MODEL

    def _build_rotation(self, available: set[str]) -> None:
        """Work out which Groq token buckets this account can actually spend.

        Groq's free tier meters tokens per minute per *model*, so every model
        on the key is a separate budget and a 429 is news about one of them
        rather than about the account. Listing them here, once, is what lets
        `_rotate_groq_model` move without a round trip to find out whether
        the next name even exists.
        """
        configured = config.GROQ_MODEL_ROTATION or [
            config.GROQ_MODEL,
            *config.GROQ_MODEL_FALLBACKS,
        ]

        skip: set[str] = set()
        vision = (config.VISION_PROVIDER or config.LLM_PROVIDER or "").lower()
        if config.GROQ_ROTATION_AVOIDS_VISION and vision == "groq":
            # Sharing a bucket with vision would undo the point of having
            # two: one screen task is a dozen framed requests, and it would
            # empty the brain's budget on the way past.
            #
            # The whole ladder is skipped, not just the configured name.
            # Vision falls back exactly as the brain does, so the model it
            # will actually land on is usually *not* the one named in the
            # config - on a key where the default vision model has been
            # retired, skipping only that name left the brain rotating onto
            # the very model vision was about to use.
            skip.add(config.GROQ_VISION_MODEL)
            skip.update(config.GROQ_VISION_FALLBACKS)

        # Availability is checked for every entry including the first. The
        # configured model is not automatically usable: `verify_model` may be
        # about to fall back from it, and `--check` builds a rotation before
        # any of that has happened.
        rotation = [
            name
            for name in [config.GROQ_MODEL, *configured]
            if name in available and name not in skip
        ]
        rotation = list(dict.fromkeys(rotation))  # first occurrence wins

        self._groq_rotation = rotation
        if len(rotation) > 1:
            log.info("Groq token buckets for this session: %s", ", ".join(rotation))
        else:
            log.debug("Only one Groq token bucket available: %s", config.GROQ_MODEL)

    def _rotate_groq_model(self) -> bool:
        """Move to the next Groq token bucket. False when there is not one.

        Wraps, so a session long enough to exhaust every bucket comes back
        round to the first - by which time its minute has passed and it has
        refilled. That is the whole reason this is a rotation rather than a
        one-way ladder.
        """
        rotation = self._groq_rotation
        if len(rotation) < 2:
            return False

        current = self.groq_model
        try:
            index = rotation.index(current)
        except ValueError:
            index = -1
        following = rotation[(index + 1) % len(rotation)]
        if following == current:
            return False

        # Neutral about *why*. This used to say "rate limited", which was
        # true at the only call site that existed and became a lie the
        # moment there was a second one - a diagnostic that rotated by hand
        # printed three rate-limit warnings for three successful requests.
        log.info("Moving to the %s token bucket", following)
        self._groq_model = following
        return True

    def _drop_groq_model(self) -> bool:
        """Forget the current bucket and move to another. False if it is the
        only one left, in which case the error is real and belongs to the
        user rather than to the rotation.
        """
        rotation = self._groq_rotation
        if self.groq_model not in rotation or len(rotation) < 2:
            return False

        dropped = self.groq_model
        index = rotation.index(dropped)
        remaining = [name for name in rotation if name != dropped]
        # The bucket *after* the one being dropped, not the first one. The
        # first is usually the model that was just rate limited into
        # rotating here, so landing back on it wastes the only other attempt
        # this turn had left.
        following = remaining[index % len(remaining)]
        log.warning(
            "%s is listed but unusable on this key; dropping it from the "
            "rotation and using %s",
            dropped,
            following,
        )
        self._groq_rotation = remaining
        self._groq_model = following
        return True

    async def _decide_elsewhere(
        self, transcript: str, extra_context: str
    ) -> ToolCall:
        """Answer this one turn with the other provider.

        Both providers are described by the same `TOOL_SPECS`, so this is the
        same request in a different dialect: whatever comes back is a
        `ToolCall` like any other and the session carries on with no seam. The
        swap is undone in the `finally`, because the point is to save one
        utterance, not to move house over a single 429.
        """
        other = self._other_provider(self.provider)
        log.warning("Rate limited on %s; trying %s for this turn", self.provider, other)
        original = self.provider
        self.provider = other
        self._failing_over = True
        try:
            if other == "gemini":
                return await self._decide_gemini(transcript, extra_context)
            return await self._decide_groq(transcript, extra_context)
        finally:
            self.provider = original
            self._failing_over = False

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
            return await self._verify_gemini_model()

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
            self._build_rotation(available)
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
                self._build_rotation(available)
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

    async def _verify_gemini_model(self) -> str:
        """Confirm the Gemini model answers, or fall back to one that does.

        Listing is not proof here, which is why this sends a real request. A
        model can appear in `/models` and still answer `generateContent` with
        404 "no longer available to new users" - `gemini-2.5-flash` does
        exactly that on accounts created after it was retired, so a catalogue
        check would have passed it and every command would then have failed.

        One near-empty generation is the cheapest question that gets a
        truthful answer. Anything that is not a 404 counts as available: a 429
        or a 503 says the model exists and is busy, and walking down the
        ladder over a busy model would land on a worse one for the session.
        """
        ladder = [config.GEMINI_MODEL] + [
            name for name in config.GEMINI_MODEL_FALLBACKS if name != config.GEMINI_MODEL
        ]
        probe = {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 1},
        }
        headers = {"x-goog-api-key": config.GEMINI_API_KEY}

        unavailable: list[str] = []
        for candidate in ladder:
            try:
                response = await self._client.post(
                    f"{config.GEMINI_BASE_URL}/models/{candidate}:generateContent",
                    json=probe,
                    headers=headers,
                    timeout=10.0,
                )
            except httpx.HTTPError as exc:
                # Cannot tell from here; let the real call decide rather than
                # discarding a model over one flaky socket.
                log.debug("Could not probe %s: %s", candidate, exc)
                return config.GEMINI_MODEL

            if response.status_code != 404:
                if candidate != config.GEMINI_MODEL:
                    log.warning(
                        "Gemini model %r unavailable on this key; using %r",
                        config.GEMINI_MODEL,
                        candidate,
                    )
                    print(
                        f"[E.V.] '{config.GEMINI_MODEL}' isn't available on your "
                        f"Gemini key. Using '{candidate}' instead."
                    )
                    # Vision reads `GEMINI_VISION_MODEL`, which is bound at
                    # import from `GEMINI_MODEL` unless the user pinned one.
                    # Left behind, it still points at the model that just
                    # 404ed - so E.V. would talk fine and go blind the moment
                    # anything asked it to look at the screen.
                    if not config.GEMINI_VISION_MODEL_PINNED:
                        config.GEMINI_VISION_MODEL = candidate
                    config.GEMINI_MODEL = candidate
                return candidate
            unavailable.append(candidate)

        raise BrainError(
            f"None of these Gemini models are available on this key: "
            f"{', '.join(unavailable)}. Set EV_GEMINI_MODEL in your .env to one "
            "that is, or switch EV_LLM_PROVIDER to groq."
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
            try:
                return await self._decide_gemini(transcript, extra_context)
            except BrainError as exc:
                if not exc.rate_limited or not self._failover_target():
                    raise
                return await self._decide_elsewhere(transcript, extra_context)

        # Groq meters tokens per minute per *model*, so a 429 is a fact
        # about one bucket, not about the key. Moving to the next bucket is
        # both cheaper and better than reaching for the other provider,
        # whose free tier is metered per day rather than per minute - one
        # hop here costs nothing, one hop there spends a scarce request.
        # Each bucket gets one attempt per turn. `tried` rather than a count
        # because the rotation can change underneath this loop - a bucket
        # that turns out not to exist is dropped from it - and because the
        # rotation wraps, so counting wrong means spending a second request
        # on a model that was rate limited moments ago.
        last: BrainError | None = None
        tried: set[str] = set()
        while True:
            tried.add(self.groq_model)
            try:
                return await self._decide_groq_turn(
                    transcript, extra_context, on_sentence
                )
            except BrainError as exc:
                if exc.model_unavailable and self._drop_groq_model():
                    # Listed by the catalogue and refused on use. Spend a
                    # different bucket rather than making it the user's
                    # problem - but only when there is another one, so a
                    # genuinely misconfigured single model still says so.
                    if self.groq_model in tried:
                        break
                    continue
                if not exc.rate_limited:
                    raise
                last = exc
                exhausted = self.groq_model
                if not self._rotate_groq_model() or self.groq_model in tried:
                    break
                log.warning(
                    "Rate limited on %s; retrying this turn on %s, which has "
                    "its own budget",
                    exhausted,
                    self.groq_model,
                )

        if last is None:  # unreachable: the loop runs at least once
            raise BrainError("The model did not answer.")

        # Every bucket on this key is empty. The other provider is the last
        # thing between the user and being told to come back later.
        if self._failover_target():
            return await self._decide_elsewhere(transcript, extra_context)
        raise last

    async def _decide_groq_turn(
        self,
        transcript: str,
        extra_context: str,
        on_sentence: SentenceHook | None,
    ) -> ToolCall:
        """One attempt at this turn against the current Groq model.

        Streaming first where it is available, then the plain call. A rate
        limit is deliberately *not* handled here: only the caller knows there
        is another token bucket to move to. Before that was true a 429 raised
        during streaming escaped `decide` altogether, taking the plain-call
        retry and provider failover with it - so the one case that had two
        remedies got neither.
        """
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

            # Groq states the delay in a header; Gemini states it in the body.
            # Checking only one of them made every Gemini rate limit look like
            # one with no stated delay, so E.V. never waited at all.
            wait = _retry_after(response.headers)
            if wait is None:
                wait = _gemini_retry_after(response.text)
            if wait is None or wait > config.LLM_RATE_LIMIT_MAX_WAIT_S:
                # Longer than anyone will stand at a microphone waiting. The
                # caller may still hand this turn to the other provider.
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
            # Flagged, so the caller can ask the other provider instead of
            # handing the user a delay they can do nothing about.
            raise BrainError(
                "I'm over my rate limit. Try that again shortly.",
                rate_limited=True,
            )
        if status == 503:
            # A busy model, not a broken request. Treated as a rate limit so
            # it takes the same failover path: waiting out "high demand" and
            # asking somebody else are the same remedy.
            raise BrainError(
                "That model's busy right now. Try that again shortly.",
                rate_limited=True,
            )
        if status >= 400 and "tool_use_failed" in body:
            # The model's own tool call was rejected, not the request.
            # Flagged so the caller can retry instead of surfacing it, and
            # phrased as English because this string is spoken aloud if
            # every retry fails - a raw JSON error body read out by a
            # speech synthesiser is the worst possible answer.
            raise BrainError(
                "That one tangled me up. Try it a simpler way?",
                tool_failure=True,
            )
        if status == 404:
            # Almost always a model name this account cannot use. The body is
            # JSON, and every BrainError message can end up at a speech
            # synthesiser, so the detail goes to the log and the user gets a
            # sentence. `verify_model` normally catches this at startup; a
            # one-shot run that skipped it lands here.
            log.warning("Model returned 404: %s", body[:400])
            raise BrainError(
                "That model isn't available on my key. Check EV_GEMINI_MODEL "
                "or EV_GROQ_MODEL in your .env file.",
                model_unavailable=True,
            )
        if status == 403:
            # Listed by the catalogue, refused on use - a model disabled in
            # the project's own settings rather than missing from the
            # account. Same remedy as a 404: spend a different bucket.
            log.warning("Model returned 403: %s", body[:400])
            raise BrainError(
                "That model is blocked on my key. Check your project's model "
                "settings, or set EV_GROQ_MODEL to another one.",
                model_unavailable=True,
            )
        if status >= 400:
            # Same reasoning: log the body, speak a sentence. Reading braces
            # and quotation marks out loud is the worst available answer.
            log.warning("Model returned %d: %s", status, body[:400])
            raise BrainError(
                f"The model sent back an error, {status}. Run with -v for the "
                "detail."
            )

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

    def _selected_tools(self, transcript: str, extra_context: str) -> list[str] | None:
        """Tool names worth offering this turn, or None meaning all of them.

        `session_context` is deliberately not part of the decision. It is
        standing state - stored facts, open backlog - and it mentions folders
        and errands and applications, so feeding it to the selector would
        match nearly every tool on nearly every turn and quietly give back
        the saving. What the user just said, and what the last action did,
        are the only things that bear on what they want done next.
        """
        if not config.TOOL_SUBSET_ENABLED:
            return None
        return select_tools(transcript, extra_context)

    def _groq_payload(
        self, transcript: str, extra_context: str, *, all_tools: bool = False
    ) -> dict[str, Any]:
        names = None if all_tools else self._selected_tools(transcript, extra_context)
        return {
            "model": self.groq_model,
            "messages": self._groq_messages(transcript, extra_context),
            "tools": to_openai_tools(names),
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
            # Two things can put us here and this rung answers both at
            # once. The model may have been unable to express what it wanted
            # in any of the tools it was shown - so the full schema goes
            # back on - or it may have wanted no tool at all, so the
            # constraint comes off. Trying them one at a time would cost an
            # extra round trip to distinguish cases that have the same fix.
            log.info("Model's tool call was rejected; retrying with the full schema")
            payload = self._groq_payload(transcript, extra_context, all_tools=True)
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
            "tools": to_gemini_tools(self._selected_tools(transcript, extra_context)),
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {
                "temperature": config.LLM_TEMPERATURE,
                "maxOutputTokens": config.LLM_MAX_TOKENS,
            },
        }
        url = f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_MODEL}:generateContent"
        headers = {"x-goog-api-key": config.GEMINI_API_KEY}

        data = await self._post(url, payload, headers)

        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts")
        if not parts:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                raise BrainError(f"Gemini blocked that request: {blocked}")
            reason = candidate.get("finishReason") or ""
            if reason == "MAX_TOKENS":
                # A thinking model can spend the whole output budget before
                # writing a single visible part, and the candidate then comes
                # back finished, valid, and empty. That is not a malformed
                # response, so it must not be reported as one - the plain
                # reply below turns it into words instead of an error.
                log.info("Gemini hit the output ceiling before answering")
                return await self._plain_reply_gemini(transcript, extra_context)
            raise BrainError(
                f"Unexpected Gemini response shape: {str(data)[:200]}"
            )

        for part in parts:
            call = part.get("functionCall")
            if call and call.get("name"):
                return ToolCall(call["name"], _coerce_arguments(call.get("args")))

        text = " ".join(part.get("text", "") for part in parts).strip()
        rescued = _salvage_tool_call(text)
        if rescued is not None:
            return rescued
        log.info("Gemini answered with prose instead of a function call")
        return ToolCall("chat", {"reply": text or "I didn't catch that."})

    async def _plain_reply_gemini(self, transcript: str, extra_context: str) -> ToolCall:
        """Ask Gemini for words, with no tools and room to write them.

        The counterpart of `_plain_reply` on the Groq side, and reached for
        the same reason: the tool-calling machinery is the thing that failed,
        so the way to get an answer is to take it away. The output ceiling is
        lifted as well, because the usual cause of getting here is a thinking
        model spending the whole budget before it says anything.
        """
        payload = {
            "systemInstruction": {
                "parts": [{"text": self._system_prompt(extra_context)}]
            },
            "contents": [{"role": "user", "parts": [{"text": transcript}]}],
            "generationConfig": {
                "temperature": config.LLM_TEMPERATURE,
                "maxOutputTokens": max(config.LLM_MAX_TOKENS, 1024),
            },
        }
        url = f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_MODEL}:generateContent"
        try:
            data = await self._post(url, payload, {"x-goog-api-key": config.GEMINI_API_KEY})
        except BrainError:
            return ToolCall(
                "chat", {"reply": "I couldn't work out how to do that one."}
            )
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = " ".join(part.get("text", "") for part in parts).strip()
        rescued = _salvage_tool_call(text)
        if rescued is not None:
            return rescued
        return ToolCall(
            "chat", {"reply": text or "I couldn't work out how to do that one."}
        )
