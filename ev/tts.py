"""Text to speech via edge-tts (Microsoft Edge neural voices).

Synthesis happens on Microsoft's servers, so the only local cost is the
HTTP/WebSocket round trip and a temporary MP3 on disk. No model, no GPU, no
resident memory beyond the audio buffer itself.

Two things this module refuses to do, because both make an assistant feel
broken:

* **Drop words.** Long replies are split on sentence boundaries and spoken in
  full, with the next chunk synthesising while the current one plays. Nothing
  is ever silently truncated.
* **Fail quietly.** If synthesis or playback fails, that reaches the terminal.
  Text appearing with no sound and no explanation is the worst outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Callable

import config
from ev.audio import build_player

log = logging.getLogger("ev.tts")


class SpeechError(RuntimeError):
    """Synthesis or playback failed."""


# Strip things that sound wrong when read aloud.
_MARKDOWN = re.compile(r"[*_`#>|~]+")
_URL = re.compile(r"https?://\S+|\bwww\.\S+")
_WHITESPACE = re.compile(r"\s+")
# Split after . ! ? when followed by a space, but not on common abbreviations
# or on the initials in "E.V." itself.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

# Meta-labels: the names of channels, roles and UI chrome, which are never
# speech. Three separate things produce them and all three reach this module:
#
# * the model, after seeing its own past replies stored with a "Spoke: "
#   prefix (fixed at source in `ev.brain`, but a model can always improvise),
# * terminal chrome such as "E.V. >" or "[E.V.]" echoed back into a reply,
# * a raw tool observation ("$ git status", "exit=0") reaching the wrong field.
#
# Saying any of them out loud is the most obviously broken thing a voice
# assistant can do, so this is a hard boundary rather than a tidy-up.
_LABEL = r"""(?:
      spoke|spoken|speaking|speech|said|saying|says
    | response|responds?|responding|repl(?:y|ies|ying)
    | answers?|answering|outputs?|results?|details?
    | assistant|ai|bot|system|user|you|me|transcript|message|text
    | notes?|actions?|tool(?:\ call)?|command|status|thought|thinking
    | e\s*\.?\s*v\s*\.?
)"""
# "[E.V.]", "(assistant)", "<system>" - with or without a trailing separator.
_META_BRACKETED = re.compile(
    rf"^\s*[\[(<]\s*{_LABEL}\s*[\])>]\s*[:>\-\u2013\u2014]?\s*",
    re.IGNORECASE | re.VERBOSE,
)
# "Spoke:", "E.V. >", "Response:". The separator is required, so an ordinary
# sentence that merely opens with one of these words is left alone.
_META_BARE = re.compile(rf"^\s*{_LABEL}\s*[:>]+\s*", re.IGNORECASE | re.VERBOSE)
# Occasionally a whole tool-call envelope arrives where the reply should be.
_JSON_REPLY = re.compile(
    r'^\s*\{.*?"(?:reply|text|speech|content|message|response)"\s*:\s*"(.*?)"\s*[,}]',
    re.DOTALL,
)
# A leaked command echo is a transcript of a command, not speech, so the
# whole line goes - stripping only the "$" would still read it aloud.
_SHELL_ECHO = re.compile(r"^[ \t]*(?:\$|PS\s+[A-Za-z]:\[^>\n]*>)[ \t]+.*$", re.MULTILINE)
_EXIT_CODE = re.compile(r"^[ \t]*exit\s*=\s*-?\d+[ \t]*$", re.MULTILINE | re.IGNORECASE)
_QUOTE_PAIRS = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}

# Typographic punctuation, flattened to ASCII.
#
# Two reasons, and the second is the one that bites. A speech synthesiser
# mostly copes with a curly apostrophe, but the Windows console is cp1252 and
# cannot encode one at all: "That's a marathon" reaches the terminal as
# "That?s a marathon". The same cleaned string is what the UI draws and what
# the speaker receives - that is deliberate, and it means a character the
# console cannot render is a visible defect even though the audio was fine.
#
# A conversational register produces these constantly, far more than a clipped
# one did, so this stopped being cosmetic the moment the voice got warmer.
_TYPOGRAPHIC = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u200b": "",
        "\u2032": "'",
        "\u2033": '"',
    }
)


def strip_meta_labels(text: str) -> str:
    """Remove role labels and channel prefixes from a would-be spoken string.

    Deliberately strict about the separator: "Spoke: hi" is a label, but
    "Spoke to your mother" is a sentence. Only the former is touched.
    """
    spoken = (text or "").strip()
    if not spoken:
        return ""

    envelope = _JSON_REPLY.match(spoken)
    if envelope:
        try:
            spoken = json.loads(f'"{envelope.group(1)}"')
        except ValueError:
            spoken = envelope.group(1)
        spoken = spoken.strip()

    spoken = _EXIT_CODE.sub(" ", spoken)
    spoken = _SHELL_ECHO.sub(" ", spoken)

    # Labels stack ("Spoke: E.V.: hi"), so peel until nothing more comes off.
    for _ in range(4):
        peeled = _META_BARE.sub("", _META_BRACKETED.sub("", spoken, count=1), count=1)
        peeled = peeled.strip()
        if peeled == spoken:
            break
        spoken = peeled

    # Models like to hand back the whole reply wrapped in quotes.
    if len(spoken) >= 2 and _QUOTE_PAIRS.get(spoken[0]) == spoken[-1]:
        spoken = spoken[1:-1].strip()
    return spoken


def clean_for_speech(text: str) -> str:
    """Make arbitrary model output safe to read aloud.

    This normalises only. It never shortens - see `split_for_speech`.

    Label stripping runs first, because removing markdown would delete the
    ">" that makes "E.V. > hi" recognisable as chrome in the first place.
    """
    spoken = strip_meta_labels(text)
    # Before anything else looks at the characters: the quote-stripping below
    # and `_QUOTE_PAIRS` both work on ASCII quotes, and flattening first means
    # a reply wrapped in curly quotes is unwrapped like any other.
    spoken = spoken.translate(_TYPOGRAPHIC)
    spoken = _URL.sub("that link", spoken)
    spoken = _MARKDOWN.sub("", spoken)
    # Bare paths read terribly character by character.
    spoken = re.sub(r"\b[A-Za-z]:\[^\s]+", "that path", spoken)
    spoken = _WHITESPACE.sub(" ", spoken).strip()
    # Peel again: stripping markdown can uncover a label that was hidden
    # behind it, as in "**E.V.:** hi".
    return strip_meta_labels(spoken)


def split_for_speech(text: str, limit: int | None = None) -> list[str]:
    """Break a reply into chunks that are each quick to synthesise.

    The first chunk is deliberately kept short. Synthesis time scales with
    text length (0.87s for a few words, 1.65s for a sentence), so a small
    opening chunk is what makes E.V. start talking sooner; the rest is
    synthesised while that plays and the seam is inaudible.

    Splits on sentence boundaries first, and only falls back to splitting on
    words for a single sentence longer than the limit. Every word survives.
    """
    limit = config.TTS_CHUNK_CHARS if limit is None else limit
    spoken = clean_for_speech(text)
    if not spoken:
        return []

    # Short enough to say in one breath: no seam, no benefit to splitting.
    if len(spoken) <= config.TTS_FIRST_CHUNK_CHARS:
        return [spoken]

    sentences = [s for s in _SENTENCE_END.split(spoken) if s.strip()]
    if len(sentences) > 1 and len(sentences[0]) <= limit:
        # Lead with the first sentence, then chunk the remainder normally.
        rest = " ".join(sentences[1:])
        return [sentences[0], *_chunk(rest, limit)]

    return _chunk(spoken, limit)


def _chunk(spoken: str, limit: int) -> list[str]:
    if len(spoken) <= limit:
        return [spoken] if spoken else []

    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(spoken):
        if not sentence.strip():
            continue
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= limit:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)

    # A single sentence longer than the limit still has to be broken up.
    final: list[str] = []
    for chunk in chunks:
        while len(chunk) > limit:
            cut = chunk.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            final.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if chunk:
            final.append(chunk)
    return final


class Speaker:
    """Serialises synthesis and playback, and supports barge-in."""

    def __init__(self) -> None:
        self.enabled = config.TTS_ENABLED
        self._player = None
        self._lock = asyncio.Lock()
        self._speaking = False
        self._cancel = False
        self._warned_once = False
        # The stream currently holding the floor, if any. `stop()` needs it:
        # killing the MP3 that is playing stops one sentence, while the rest
        # of the reply is already queued behind it and plays straight after.
        # Barge-in has to reach both.
        self._stream: "SpeechStream | None" = None
        # Called as playback starts, so the caller can forget whatever the
        # microphone heard a moment ago. Without it the tail of the user's own
        # command counts as recent speech and E.V. barges in on itself.
        #
        # Annotated, because `= None` on its own makes the attribute's type
        # None, and the one thing anybody ever does with this is assign a
        # function to it.
        self.on_playback_start: Callable[[], None] | None = None

        if not self.enabled:
            return
        try:
            self._player = build_player()
        except Exception as exc:
            print(f"[E.V.] No audio playback available ({exc}). Running text-only.")
            log.warning("No audio playback available: %s", exc)
            self.enabled = False

    @property
    def speaking(self) -> bool:
        return self._speaking

    async def warmup(self) -> None:
        """Pay the import and TLS costs before the first reply, not during it.

        Importing edge_tts pulls in aiohttp and costs around three seconds on a
        cold interpreter. Left lazy, that lands on the user's first command.
        """
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(__import__, "edge_tts")
        except Exception as exc:
            print(f"[E.V.] edge-tts unavailable ({exc}). Running text-only.")
            log.warning("edge-tts could not be imported: %s", exc)
            self.enabled = False

    async def say(self, text: str) -> str:
        """Speak `text` in full. Returns what was said, cleaned."""
        chunks = split_for_speech(text)
        if not chunks:
            return ""
        spoken = " ".join(chunks)
        if not self.enabled or self._player is None:
            return spoken

        async with self._lock:
            self._speaking = True
            self._cancel = False
            self._note_playback_start()
            try:
                await self._speak_chunks(chunks)
            finally:
                self._speaking = False
        return spoken

    def _note_playback_start(self) -> None:
        """Tell the caller the floor has just been taken. Never raises."""
        hook = self.on_playback_start
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:  # a bad hook must not cost the user speech
            log.debug("Playback-start hook failed: %s", exc)

    async def _speak_chunks(self, chunks: list[str]) -> None:
        """Play each chunk, synthesising the next one while it plays.

        The overlap matters: without it every sentence boundary in a long reply
        would add most of a second of dead air.
        """
        pending = asyncio.ensure_future(self._synthesise(chunks[0]))
        for index, _ in enumerate(chunks):
            try:
                path = await pending
            except Exception as exc:
                self._report_failure(exc)
                return

            next_task = None
            if index + 1 < len(chunks) and not self._cancel:
                next_task = asyncio.ensure_future(self._synthesise(chunks[index + 1]))
            pending = next_task

            if self._cancel:
                _unlink(path)
                break

            try:
                await asyncio.to_thread(
                    self._player.play, path, True, config.TTS_TIMEOUT_S + 30
                )
            except Exception as exc:
                self._report_failure(exc)
                break
            finally:
                _unlink(path)

            if self._cancel:
                break

        if pending is not None:
            pending.cancel()
            try:
                leftover = await pending
            except (asyncio.CancelledError, Exception):
                leftover = None
            if leftover:
                _unlink(leftover)

    def _report_failure(self, exc: BaseException) -> None:
        """Surface a speech failure once, loudly, instead of going mute."""
        log.warning("Speech failed: %s", exc)
        if not self._warned_once:
            self._warned_once = True
            print(f"[E.V.] Voice output failed ({exc}). Text still works; run -v for detail.")

    async def prewarm(self, phrases: list[str]) -> None:
        """Synthesise E.V.'s stock replies ahead of time, into the cache.

        Control phrases like "Standing by." come from a fixed pool, so paying
        the ~0.8s round trip for them at startup means they play instantly
        later - which is the whole point of matching those intents locally.
        """
        if not self.enabled:
            return
        warmed_player = False
        for phrase in phrases:
            cached = self._cached(phrase)
            if cached is None:
                try:
                    path = await self._synthesise(phrase, _retry=False)
                except Exception as exc:
                    log.debug("Prewarm failed for %r: %s", phrase, exc)
                    return  # offline or rate limited; not worth hammering
                self._store(phrase, path)
                cached = self._cached(phrase) or path
            # Load the OS MP3 codec once, so the first real reply does not.
            if not warmed_player and hasattr(self._player, "warm"):
                await asyncio.to_thread(self._player.warm, cached)
                warmed_player = True
            if cached and not cached.startswith(str(config.TTS_CACHE_DIR)):
                _unlink(cached)

    # -- cache ------------------------------------------------------------
    def _cache_key(self, text: str) -> str:
        """Voice settings are part of the key, so changing them never replays
        stale audio in the old voice."""
        signature = "|".join(
            (
                text,
                config.TTS_VOICE,
                config.TTS_RATE,
                config.TTS_VOLUME,
                config.TTS_PITCH,
            )
        )
        return hashlib.sha1(signature.encode("utf-8")).hexdigest()[:20]

    def _cached(self, text: str) -> str | None:
        if not config.TTS_CACHE_ENABLED:
            return None
        path = config.TTS_CACHE_DIR / f"{self._cache_key(text)}.mp3"
        if path.exists() and path.stat().st_size > 256:
            return str(path)
        return None

    def _store(self, text: str, source: str) -> None:
        if not config.TTS_CACHE_ENABLED:
            return
        try:
            config.TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            target = config.TTS_CACHE_DIR / f"{self._cache_key(text)}.mp3"
            shutil.copyfile(source, target)
            self._evict_if_needed()
        except OSError as exc:
            log.debug("Could not cache speech: %s", exc)

    def _evict_if_needed(self) -> None:
        """Keep the cache to a fixed number of least-recently-used clips."""
        try:
            clips = sorted(
                config.TTS_CACHE_DIR.glob("*.mp3"), key=lambda p: p.stat().st_atime
            )
        except OSError:
            return
        for stale in clips[: max(0, len(clips) - config.TTS_CACHE_MAX_FILES)]:
            try:
                stale.unlink()
            except OSError:
                pass

    async def _synthesise(self, text: str, _retry: bool = True) -> str:
        cached = self._cached(text)
        if cached is not None:
            # Copy out, because the caller unlinks whatever it is handed.
            handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            handle.close()
            try:
                shutil.copyfile(cached, handle.name)
                return handle.name
            except OSError:
                _unlink(handle.name)

        try:
            import edge_tts
        except ImportError as exc:
            raise SpeechError("edge-tts is not installed: pip install edge-tts") from exc

        handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        path = handle.name
        handle.close()

        communicate = edge_tts.Communicate(
            text,
            voice=config.TTS_VOICE,
            rate=config.TTS_RATE,
            volume=config.TTS_VOLUME,
            pitch=config.TTS_PITCH,
        )
        try:
            await asyncio.wait_for(communicate.save(path), timeout=config.TTS_TIMEOUT_S)
        except asyncio.CancelledError:
            _unlink(path)
            raise
        except Exception as exc:
            _unlink(path)
            # Edge TTS drops the occasional WebSocket. One retry turns a silent
            # reply into a slightly late one.
            if _retry:
                log.info("Retrying synthesis after: %s", exc)
                await asyncio.sleep(0.2)
                return await self._synthesise(text, _retry=False)
            raise SpeechError(f"edge-tts failed: {exc}") from exc

        if not os.path.exists(path) or os.path.getsize(path) < 256:
            _unlink(path)
            if _retry:
                return await self._synthesise(text, _retry=False)
            raise SpeechError(f"edge-tts produced no audio for voice {config.TTS_VOICE}")

        # Short replies repeat constantly ("Done.", "Standing by."). Caching
        # them turns the next occurrence into a file copy.
        if len(text) <= config.TTS_CACHE_MAX_CHARS:
            self._store(text, path)
        return path

    def stream(self) -> "SpeechStream":
        """Open a stream that speaks sentences as they are handed over.

        This is what removes the dead air after the user stops talking. The
        old path waited for the whole reply, then synthesised, then played.
        Here the first sentence is already playing while the model is still
        writing the second.
        """
        stream = SpeechStream(self)
        self._stream = stream
        return stream

    def stop(self) -> None:
        """Cut playback short, for barge-in or shutdown.

        Three things have to stop, not one. Killing the clip that is playing
        only ends the current sentence; a streamed reply has the rest of
        itself queued behind that clip and would carry straight on into it,
        which is the opposite of yielding the floor. So: refuse further
        chunks, empty the stream's queue, then kill the audio device.
        """
        self._cancel = True
        # Before the player, so nothing new can be queued in the window
        # between killing the clip and the stream noticing it should stop.
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.cancel()
            except Exception as exc:
                log.debug("Could not cancel the speech stream: %s", exc)
        if self._player is not None:
            try:
                self._player.stop()
            except Exception as exc:
                log.debug("Stop failed: %s", exc)
        self._speaking = False


class SpeechStream:
    """A speech pipeline fed one sentence at a time.

    Sentences are queued as they arrive and played in order. While one plays,
    the next is already being synthesised, so the seam between them is
    inaudible - the same overlap `Speaker._speak_chunks` uses, but driven by a
    producer that has not finished writing yet.

    `feed` never blocks the caller, which matters because the caller is the
    SSE read loop: stalling it would stall the very generation being spoken.
    """

    def __init__(self, speaker: Speaker) -> None:
        self._speaker = speaker
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._spoken: list[str] = []
        self._closed = False
        self._cancelled = False
        self._task: asyncio.Task | None = None
        if speaker.enabled and speaker._player is not None:
            self._task = asyncio.ensure_future(self._run())

    @property
    def spoken(self) -> str:
        """Everything handed to this stream, as one string."""
        return " ".join(self._spoken).strip()

    def feed(self, sentence: str) -> None:
        """Queue one sentence. Non-blocking, and safe to call from a hook."""
        spoken = clean_for_speech(sentence)
        # `_closed` covers a normal end of generation; `_cancelled` covers
        # barge-in, where the model is still streaming sentences at a hook
        # that must no longer accept them.
        if not spoken or self._closed or self._cancelled:
            return
        self._spoken.append(spoken)
        if self._task is not None:
            self._queue.put_nowait(spoken)

    def close(self) -> None:
        """Signal that no more sentences are coming."""
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._queue.put_nowait(None)

    async def finish(self) -> str:
        """Close the stream and wait for everything queued to finish playing."""
        self.close()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        return self.spoken

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Abandon playback, for barge-in or shutdown.

        The queue is emptied rather than just closed. A sentence still sitting
        in it is a sentence E.V. is about to say, and "stop talking" that
        leaves three queued sentences to play is not a stop.
        """
        self._closed = True
        self._cancelled = True
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._task is not None:
            self._queue.put_nowait(None)
            self._task.cancel()

    async def _run(self) -> None:
        speaker = self._speaker
        async with speaker._lock:
            speaker._speaking = True
            speaker._cancel = False
            speaker._note_playback_start()
            synth: asyncio.Future | None = None
            path: str | None = None
            try:
                while not speaker._cancel:
                    if synth is None:
                        text = await self._queue.get()
                        if text is None:
                            break
                        synth = asyncio.ensure_future(speaker._synthesise(text))

                    try:
                        path = await synth
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        speaker._report_failure(exc)
                        synth = None
                        if self._closed and self._queue.empty():
                            break
                        continue
                    synth = None

                    # If the next sentence has already landed, start
                    # synthesising it now so it overlaps with this playback.
                    if not self._queue.empty():
                        nxt = self._queue.get_nowait()
                        if nxt is None:
                            self._closed = True
                        else:
                            synth = asyncio.ensure_future(speaker._synthesise(nxt))

                    if speaker._cancel:
                        break

                    try:
                        await asyncio.to_thread(
                            speaker._player.play, path, True, config.TTS_TIMEOUT_S + 30
                        )
                    except Exception as exc:
                        speaker._report_failure(exc)
                        break
                    finally:
                        _unlink(path)
                        path = None

                    if self._closed and synth is None and self._queue.empty():
                        break
            finally:
                speaker._speaking = False
                if speaker._stream is self:
                    speaker._stream = None
                _unlink(path)
                if synth is not None:
                    synth.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        leftover = await synth
                        _unlink(leftover)


def _unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


async def list_voices(prefix: str = "en-") -> list[str]:
    """Available Edge voice names, for picking one to put in .env."""
    import edge_tts

    voices = await edge_tts.list_voices()
    return sorted(
        voice["ShortName"]
        for voice in voices
        if voice["ShortName"].startswith(prefix)
    )
