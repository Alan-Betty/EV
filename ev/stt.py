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


class Transcriber:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.provider = config.STT_PROVIDER
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.STT_TIMEOUT_S)

        if self.provider == "groq" and not config.GROQ_API_KEY:
            raise TranscriptionError(
                "GROQ_API_KEY is required for the groq STT backend. "
                "Set EV_STT_PROVIDER=google to use the keyless fallback."
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def transcribe(self, wav_bytes: bytes) -> str:
        """Return the transcript, or an empty string if it was not speech."""
        if self.provider == "google":
            text = await asyncio.to_thread(self._google, wav_bytes)
        elif self.provider == "whispercpp":
            text = await asyncio.to_thread(self._whisper_cpp, wav_bytes)
        else:
            text = await self._groq(wav_bytes)

        text = text.strip()
        if _is_noise(text):
            log.debug("Discarded likely-noise transcript: %r", text)
            return ""
        return text

    # -- Groq Cloud Whisper ----------------------------------------------
    async def _groq(self, wav_bytes: bytes) -> str:
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {
            "model": config.GROQ_STT_MODEL,
            "response_format": "json",
            "temperature": "0",
            # Whisper conditions on this text, which pulls its output towards
            # the vocabulary E.V. actually hears. Without it "E.V." comes back
            # as "Eevee", "VS Code" as "the escode", and app names get mangled
            # into ordinary English words.
            "prompt": config.STT_VOCABULARY,
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
            return response.json().get("text", "")
        except ValueError as exc:
            raise TranscriptionError("Transcription response was not JSON.") from exc

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
