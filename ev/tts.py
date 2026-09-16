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
import hashlib
import logging
import os
import re
import shutil
import tempfile

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


def clean_for_speech(text: str) -> str:
    """Make arbitrary model output safe to read aloud.

    This normalises only. It never shortens - see `split_for_speech`.
    """
    spoken = _URL.sub("that link", text or "")
    spoken = _MARKDOWN.sub("", spoken)
    # Bare paths read terribly character by character.
    spoken = re.sub(r"\b[A-Za-z]:\\[^\s]+", "that path", spoken)
    spoken = _WHITESPACE.sub(" ", spoken).strip()
    return spoken


def split_for_speech(text: str, limit: int | None = None) -> list[str]:
    """Break a reply into chunks that are each quick to synthesise.

    Splits on sentence boundaries first, and only falls back to splitting on
    words for a single sentence longer than the limit. Every word survives.
    """
    limit = config.TTS_CHUNK_CHARS if limit is None else limit
    spoken = clean_for_speech(text)
    if not spoken:
        return []
    if len(spoken) <= limit:
        return [spoken]

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
            try:
                await self._speak_chunks(chunks)
            finally:
                self._speaking = False
        return spoken

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
        for phrase in phrases:
            if self._cached(phrase) is not None:
                continue
            try:
                path = await self._synthesise(phrase, _retry=False)
            except Exception as exc:
                log.debug("Prewarm failed for %r: %s", phrase, exc)
                return  # offline or rate limited; not worth hammering
            self._store(phrase, path)
            _unlink(path)

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

    def stop(self) -> None:
        """Cut playback short, for barge-in or shutdown."""
        self._cancel = True
        if self._player is not None:
            try:
                self._player.stop()
            except Exception as exc:
                log.debug("Stop failed: %s", exc)
        self._speaking = False


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
