"""Speech to text.

Three backends, all cheap on local memory:

* ``groq``      - Groq Cloud Whisper. Fast, accurate, free tier. Default.
* ``google``    - SpeechRecognition's free Google Web Speech endpoint. No key.
* ``whispercpp``- a local whisper.cpp binary, for offline use. Only this one
  costs real RAM, and only as a separate short-lived process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile

import httpx

import config

log = logging.getLogger("ev.stt")


class TranscriptionError(RuntimeError):
    """Audio could not be turned into text."""


# Whisper emits these when handed silence or noise. Treating them as speech
# makes the assistant respond to an empty room.
_HALLUCINATIONS = {
    "",
    ".",
    "you",
    "thank you.",
    "thanks for watching!",
    "thank you for watching!",
    "thank you for watching.",
    "please subscribe!",
    "bye.",
    "[blank_audio]",
    "[ silence ]",
    "(silence)",
    "subtitles by the amara.org community",
}


def _is_noise(text: str) -> bool:
    return text.strip().lower().strip("!.,") in {
        item.strip().lower().strip("!.,") for item in _HALLUCINATIONS
    }


class Transcript(str):
    """A transcript that carries the recogniser's own confidence in itself.

    Whisper knows perfectly well when it was guessing, and until now E.V. threw
    that away by asking for plain `json`. The cost of discarding it is not a
    wrong word on screen - it is a *wrong action*: a mangled transcript still
    gets handed to the model, which dutifully picks a tool and runs it. Asking
    for `verbose_json` costs nothing extra on the same free endpoint and turns
    that failure into a re-ask.

    A `str` subclass, so every existing call site - `.lower()`, truthiness,
    f-strings, `match_intent` - keeps working untouched, and only the code
    that cares about confidence has to know this is more than text.

    Note that `str` methods return plain `str`, so the metadata does not
    survive `.strip()`. Read it before slicing, which is what `_tick` does.
    """

    avg_logprob: float
    no_speech: float
    compression: float

    def __new__(
        cls,
        text: str = "",
        avg_logprob: float = 0.0,
        no_speech: float = 0.0,
        compression: float = 0.0,
    ) -> "Transcript":
        obj = super().__new__(cls, text)
        obj.avg_logprob = avg_logprob
        obj.no_speech = no_speech
        obj.compression = compression
        return obj

    @property
    def scored(self) -> bool:
        """False for backends that report no confidence at all."""
        return self.avg_logprob != 0.0 or self.no_speech != 0.0

    @property
    def rejected(self) -> bool:
        """Too unreliable to act on. Ask again rather than guess."""
        if not config.STT_CONFIDENCE_GATE or not self.scored:
            return False
        return (
            self.avg_logprob < config.STT_MIN_LOGPROB
            or self.no_speech > config.STT_MAX_NO_SPEECH
            # Whisper's classic failure is looping a phrase until the buffer
            # ends. The text compresses absurdly well when that happens.
            or self.compression > config.STT_MAX_COMPRESSION
        )

    @property
    def uncertain(self) -> bool:
        """Worth acting on, but worth telling the model it was unclear."""
        if not config.STT_CONFIDENCE_GATE or not self.scored or self.rejected:
            return False
        return self.avg_logprob < config.STT_UNCERTAIN_LOGPROB

    def why(self) -> str:
        """One line for the terminal, explaining a rejection."""
        return (
            f"logprob={self.avg_logprob:.2f} "
            f"no_speech={self.no_speech:.2f} "
            f"compression={self.compression:.2f}"
        )


def _score(payload: dict) -> tuple[float, float, float]:
    """Pull confidence out of a `verbose_json` response.

    Averaged across segments by duration, because a long confident sentence
    followed by a half-second of mumbling should not read as half-bad.
    """
    segments = payload.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return 0.0, 0.0, 0.0

    total = 0.0
    weighted = 0.0
    worst_silence = 0.0
    worst_compression = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            span = max(
                0.01, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0))
            )
            weighted += float(segment.get("avg_logprob", 0.0)) * span
            total += span
            worst_silence = max(worst_silence, float(segment.get("no_speech_prob", 0.0)))
            worst_compression = max(
                worst_compression, float(segment.get("compression_ratio", 0.0))
            )
        except (TypeError, ValueError):
            continue

    return (weighted / total if total else 0.0), worst_silence, worst_compression


class Transcriber:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.provider = config.STT_PROVIDER
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.STT_TIMEOUT_S)
        # Words this particular machine is likely to hear. Filled by the core
        # loop, which is the only layer that knows about installed apps and
        # the backlog; `ev.stt` stays below `tools` and does not reach up.
        self._hints: list[str] = []
        self._recent = ""

        if self.provider == "groq" and not config.GROQ_API_KEY:
            raise TranscriptionError(
                "GROQ_API_KEY is required for the groq STT backend. "
                "Set EV_STT_PROVIDER=google to use the keyless fallback."
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- decoding hints ---------------------------------------------------
    def set_hints(self, words: list[str]) -> None:
        """Words worth biasing the recogniser towards on this machine.

        Whisper conditions its decoding on the prompt, which makes this the
        cheapest accuracy improvement available: it is what turns "Eevee" into
        "E.V." and stops the name of an installed program being rewritten into
        the nearest ordinary English word. A static list in config cannot know
        what the user installed last week; this can.
        """
        seen: set[str] = set()
        cleaned: list[str] = []
        for word in words:
            text = " ".join(str(word or "").split())[:40].strip(" .,")
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                cleaned.append(text)
        self._hints = cleaned

    def note_transcript(self, text: str) -> None:
        """Remember the last thing heard, as context for the next utterance.

        Whisper treats the prompt as text immediately preceding the audio, so
        the previous sentence genuinely helps it decode the next one - names
        and jargon carry across a conversation.
        """
        self._recent = " ".join(str(text or "").split())[:200]

    def _prompt(self) -> str:
        """The decoding prompt, trimmed to what Whisper will actually read.

        Whisper's prompt window is about 224 tokens and it silently drops the
        front of anything longer. Since it weights the *end* most heavily -
        that being the text nearest the audio - the order here is deliberate:
        the fixed vocabulary first, machine-specific names next, and whatever
        was just said last.
        """
        base = config.STT_VOCABULARY
        if not config.STT_DYNAMIC_PROMPT:
            return base

        budget = config.STT_PROMPT_MAX_CHARS - len(base) - len(self._recent) - 4
        extras: list[str] = []
        for hint in self._hints:
            if budget - len(hint) - 2 < 0:
                break
            extras.append(hint)
            budget -= len(hint) + 2

        parts = [base]
        if extras:
            parts.append(", ".join(extras) + ".")
        if self._recent:
            parts.append(self._recent)
        return " ".join(parts)

    async def transcribe(self, wav_bytes: bytes) -> Transcript:
        """Return the transcript, or an empty one if it was not speech.

        The other two backends report no confidence, so their transcripts come
        back unscored and the gate leaves them alone.
        """
        if self.provider == "google":
            text = await asyncio.to_thread(self._google, wav_bytes)
            result = Transcript(text)
        elif self.provider == "whispercpp":
            text = await asyncio.to_thread(self._whisper_cpp, wav_bytes)
            result = Transcript(text)
        else:
            result = await self._groq(wav_bytes)

        if _is_noise(result):
            log.debug("Discarded likely-noise transcript: %r", str(result))
            return Transcript("")
        if result.rejected:
            log.info("Low-confidence transcript %r (%s)", str(result), result.why())
        return result

    # -- Groq Cloud Whisper ----------------------------------------------
    async def _groq(self, wav_bytes: bytes) -> Transcript:
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {
            "model": config.GROQ_STT_MODEL,
            # `verbose_json` costs nothing extra on the same endpoint and is
            # the only way to find out how sure Whisper was. Without it a
            # garbled transcript becomes a confidently wrong action.
            "response_format": "verbose_json",
            "temperature": "0",
            # Whisper conditions on this text, which pulls its output towards
            # the vocabulary E.V. actually hears. Without it "E.V." comes back
            # as "Eevee", "VS Code" as "the escode", and app names get mangled
            # into ordinary English words.
            "prompt": self._prompt(),
        }
        if config.STT_LANGUAGE:
            data["language"] = config.STT_LANGUAGE

        try:
            response = await self._client.post(
                f"{config.GROQ_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                files=files,
                data=data,
            )
        except httpx.TimeoutException as exc:
            raise TranscriptionError("Transcription timed out.") from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"Network error during transcription: {exc}") from exc

        if response.status_code == 401:
            raise TranscriptionError("Groq rejected the API key.")
        if response.status_code == 429:
            raise TranscriptionError("Groq rate limit hit.")
        if response.status_code >= 400:
            raise TranscriptionError(
                f"Groq transcription returned {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise TranscriptionError("Transcription response was not JSON.") from exc

        avg_logprob, no_speech, compression = _score(payload)
        return Transcript(
            str(payload.get("text", "")).strip(), avg_logprob, no_speech, compression
        )

    # -- SpeechRecognition / Google Web Speech ----------------------------
    def _google(self, wav_bytes: bytes) -> str:
        try:
            import speech_recognition as sr
        except ImportError as exc:
            raise TranscriptionError(
                "SpeechRecognition is not installed: pip install SpeechRecognition"
            ) from exc

        recognizer = sr.Recognizer()
        audio = sr.AudioData(
            _wav_payload(wav_bytes), sample_rate=config.SAMPLE_RATE, sample_width=2
        )
        try:
            return recognizer.recognize_google(audio, language=config.STT_LANGUAGE or "en-US")
        except sr.UnknownValueError:
            return ""  # genuinely unintelligible, not an error worth surfacing
        except sr.RequestError as exc:
            raise TranscriptionError(f"Google Speech API error: {exc}") from exc

    # -- local whisper.cpp ------------------------------------------------
    def _whisper_cpp(self, wav_bytes: bytes) -> str:
        binary = config.WHISPER_CPP_BIN
        model = config.WHISPER_CPP_MODEL
        if not os.path.exists(model):
            raise TranscriptionError(f"whisper.cpp model not found at {model}")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(wav_bytes)
            path = handle.name
        try:
            result = subprocess.run(
                [binary, "-m", model, "-f", path, "-nt", "-np", "-l", config.STT_LANGUAGE or "en"],
                capture_output=True,
                text=True,
                timeout=config.STT_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            raise TranscriptionError(f"whisper.cpp binary not found: {binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError("whisper.cpp timed out.") from exc
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if result.returncode != 0:
            raise TranscriptionError(f"whisper.cpp failed: {result.stderr[:200]}")
        return " ".join(line.strip() for line in result.stdout.splitlines() if line.strip())


def _wav_payload(wav_bytes: bytes) -> bytes:
    """Strip the WAV header back off; SpeechRecognition wants raw PCM."""
    import io
    import wave

    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        return handle.readframes(handle.getnframes())
