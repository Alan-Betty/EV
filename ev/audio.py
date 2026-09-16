"""Microphone capture with energy-based VAD, and MP3 playback.

Capture prefers `sounddevice` (PortAudio, small and reliable on Python 3.13)
and falls back to PyAudio through SpeechRecognition when that is what is
installed.

Playback goes through Windows' own `winmm` MCI interface via `ctypes`. That
decodes MP3 in the OS, so E.V. needs no audio library, no ffmpeg and no
pygame - the difference is tens of megabytes of resident memory.
"""

from __future__ import annotations

import io
import logging
import math
import os
import queue
import struct
import threading
import time
import wave
from dataclasses import dataclass

import config
from tools.base import IS_WINDOWS

log = logging.getLogger("ev.audio")


class AudioError(RuntimeError):
    """No usable microphone or playback device."""


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _rms(frame: bytes) -> float:
    """Root-mean-square level of a little-endian int16 frame, normalised 0-1."""
    count = len(frame) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", frame[: count * 2])
    total = sum(sample * sample for sample in samples)
    return math.sqrt(total / count) / 32768.0


def normalise(pcm: bytes, target_peak: float = 0.85) -> bytes:
    """Scale an utterance up so its loudest sample sits near full scale.

    Speech recognisers are markedly more accurate on a well-levelled signal,
    and a headset mic a foot from the speaker's mouth routinely peaks at a
    tenth of full scale. Gain is capped so a near-silent clip is not amplified
    into pure noise.
    """
    count = len(pcm) // 2
    if count == 0:
        return pcm
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    peak = max(abs(sample) for sample in samples)
    if peak == 0:
        return pcm

    gain = (target_peak * 32767.0) / peak
    if gain <= 1.05:
        return pcm  # already loud enough; leave it alone
    gain = min(gain, config.AUDIO_MAX_GAIN)

    scaled = bytearray(count * 2)
    struct.pack_into(
        f"<{count}h",
        scaled,
        0,
        *(max(-32768, min(32767, int(sample * gain))) for sample in samples),
    )
    return bytes(scaled)


def pcm_to_wav(pcm: bytes, sample_rate: int = None) -> bytes:
    """Wrap raw mono int16 PCM in a WAV container for upload."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate or config.SAMPLE_RATE)
        handle.writeframes(pcm)
    return buffer.getvalue()


@dataclass
class Utterance:
    pcm: bytes
    duration_s: float
    # The device may not have supported the configured rate, so the real one
    # travels with the audio and lands in the WAV header.
    sample_rate: int = 0

    @property
    def wav(self) -> bytes:
        payload = normalise(self.pcm) if config.AUDIO_NORMALISE else self.pcm
        return pcm_to_wav(payload, self.sample_rate or config.SAMPLE_RATE)


class Microphone:
    """Microphone with a background reader thread and a frame queue.

    The reader runs continuously and never stops, which is what makes
    barge-in possible: E.V. can still hear the user while it is talking, so
    "E.V., take five" lands mid-sentence instead of waiting for the reply to
    finish. A blocking `listen()` that owned the device could not do that.

    The queue is bounded and drops the oldest frames when nobody is
    consuming, so a long pause costs a fixed amount of memory rather than
    growing without limit.
    """

    def __init__(self) -> None:
        self.sample_rate = config.SAMPLE_RATE
        self.frame_samples = int(self.sample_rate * config.FRAME_MS / 1000)
        self.frame_bytes = self.frame_samples * 2
        self.noise_floor = config.VAD_THRESHOLD
        self._backend = ""
        self._stream = None
        self._pyaudio = None
        self._lock = threading.Lock()
        # Roughly ten seconds of frames; far more than any consumer needs.
        self._queue: "queue.Queue[tuple[bytes, float]]" = queue.Queue(
            maxsize=max(64, int(10_000 / config.FRAME_MS))
        )
        self._reader: threading.Thread | None = None
        self._reading = False
        self._recent_speech = 0
        self._muted = False

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        if self._stream is not None:
            return
        opened = False
        try:
            self._open_sounddevice()
            self._backend = "sounddevice"
            opened = True
        except Exception as exc:
            log.debug("sounddevice unavailable: %s", exc)
        if not opened:
            try:
                self._open_pyaudio()
                self._backend = "pyaudio"
                opened = True
            except Exception as exc:
                log.debug("pyaudio unavailable: %s", exc)
        if not opened:
            raise AudioError(
                "No microphone backend. Install sounddevice: pip install sounddevice"
            )

        self._reading = True
        self._reader = threading.Thread(
            target=self._read_loop, name="ev-mic-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        """Pull frames off the device forever and queue them."""
        consecutive_errors = 0
        while self._reading:
            try:
                frame = self._read_frame_raw()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors > 20:
                    log.error("Microphone read failed repeatedly: %s", exc)
                    self._reading = False
                    break
                time.sleep(0.05)
                continue

            level = _rms(frame)
            if level > self.noise_floor:
                self._recent_speech = min(self._recent_speech + 1, 100)
            else:
                self._recent_speech = max(self._recent_speech - 1, 0)

            if self._muted:
                continue
            try:
                self._queue.put_nowait((frame, level))
            except queue.Full:
                # Nobody is listening; discard the oldest frame and keep going.
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait((frame, level))
                except queue.Empty:
                    pass

    def flush(self) -> None:
        """Discard buffered audio, so a new listen starts from now."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def speech_energy(self) -> int:
        """How many recent frames looked like speech. Used for barge-in."""
        return self._recent_speech

    def _resolve_device(self, devices) -> int | None:
        """Match EV_INPUT_DEVICE against an index or a name substring."""
        wanted = config.INPUT_DEVICE.strip()
        if not wanted:
            return None
        if wanted.isdigit():
            return int(wanted)
        needle = wanted.lower()
        for index, device in enumerate(devices):
            name = device.get("name", "") if isinstance(device, dict) else str(device)
            if needle in name.lower():
                return index
        log.warning("No input device matching %r; using the default", wanted)
        return None

    def _open_sounddevice(self) -> None:
        """Open the best available input, tolerating a bad system default.

        Windows reports a default input index of -1 when the previous default
        (a Bluetooth headset, typically) has gone away, and PortAudio then
        refuses to open anything. Falling back to the first input that actually
        opens turns "E.V. is broken" into "E.V. picked the other microphone".
        """
        import sounddevice as sd

        devices = sd.query_devices()
        candidates: list[int | None] = []

        explicit = self._resolve_device(devices)
        if explicit is not None:
            candidates.append(explicit)
        else:
            default = sd.default.device
            index = default[0] if isinstance(default, (list, tuple)) else default
            if isinstance(index, int) and index >= 0:
                candidates.append(None)  # let PortAudio use its own default

        candidates += [
            index
            for index, device in enumerate(devices)
            if device.get("max_input_channels", 0) > 0
        ]

        errors: list[str] = []
        for device in candidates:
            for rate in self._rate_candidates(devices, device):
                try:
                    stream = sd.RawInputStream(
                        samplerate=rate,
                        blocksize=int(rate * config.FRAME_MS / 1000),
                        device=device,
                        dtype="int16",
                        channels=1,
                    )
                    stream.start()
                except Exception as exc:
                    errors.append(f"device={device} rate={rate}: {exc}")
                    continue

                self._stream = stream
                self.sample_rate = int(rate)
                self.frame_samples = int(rate * config.FRAME_MS / 1000)
                self.frame_bytes = self.frame_samples * 2
                name = (
                    devices[device]["name"]
                    if isinstance(device, int)
                    else "system default"
                )
                if device is not None and device != explicit:
                    log.info("Using input device [%s] %s", device, name)
                log.debug("Input open at %d Hz on %s", self.sample_rate, name)
                return

        raise AudioError(
            "Could not open any microphone. Tried: " + "; ".join(errors[:4])
        )

    def _rate_candidates(self, devices, device) -> list[int]:
        """Preferred sample rate first, then whatever the device natively wants."""
        rates = [config.SAMPLE_RATE]
        if isinstance(device, int):
            try:
                native = int(devices[device]["default_samplerate"])
                if native and native not in rates:
                    rates.append(native)
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        for fallback in (48000, 44100, 16000):
            if fallback not in rates:
                rates.append(fallback)
        return rates

    def _open_pyaudio(self) -> None:
        import pyaudio  # type: ignore

        self._pyaudio = pyaudio.PyAudio()
        index = None
        if config.INPUT_DEVICE.strip():
            devices = [
                self._pyaudio.get_device_info_by_index(i)
                for i in range(self._pyaudio.get_device_count())
            ]
            index = self._resolve_device(devices)
        self._stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            input=True,
            input_device_index=index,
            frames_per_buffer=self.frame_samples,
        )

    def close(self) -> None:
        self._reading = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop() if self._backend == "sounddevice" else self._stream.stop_stream()
                    self._stream.close()
                except Exception as exc:
                    log.debug("Error closing stream: %s", exc)
                self._stream = None
            if self._pyaudio is not None:
                try:
                    self._pyaudio.terminate()
                except Exception:
                    pass
                self._pyaudio = None

    def __enter__(self) -> "Microphone":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- reading ----------------------------------------------------------
    def _read_frame_raw(self) -> bytes:
        """Read one frame straight off the device. Reader thread only."""
        if self._stream is None:
            raise AudioError("Microphone is not open")
        if self._backend == "sounddevice":
            data, overflowed = self._stream.read(self.frame_samples)
            if overflowed:
                log.debug("Input overflow; dropped samples")
            return bytes(data)
        return self._stream.read(self.frame_samples, exception_on_overflow=False)

    def next_frame(self, timeout: float = 1.0) -> tuple[bytes, float] | None:
        """Take the next queued frame and its level."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def calibrate(self, seconds: float | None = None) -> float:
        """Measure ambient noise so the VAD threshold suits the actual room."""
        seconds = config.VAD_CALIBRATE_S if seconds is None else seconds
        frames = max(1, int(seconds * 1000 / config.FRAME_MS))
        levels = []
        for _ in range(frames):
            item = self.next_frame(timeout=1.0)
            if item is None:
                break
            levels.append(item[1])
        if levels:
            ambient = sum(levels) / len(levels)
            self.noise_floor = max(
                config.VAD_THRESHOLD,
                min(ambient * config.VAD_NOISE_MULTIPLIER, config.VAD_MAX_FLOOR),
            )
        log.info("VAD threshold set to %.4f", self.noise_floor)
        return self.noise_floor

    def _drift_noise_floor(self, level: float) -> None:
        """Slowly track ambient noise while nobody is talking.

        A one-pole filter, biased so the floor rises quickly when the room gets
        louder but falls slowly. Falling fast would let the tail of the user's
        own speech drag the threshold down and re-trigger on itself.
        """
        ambient = level * config.VAD_NOISE_MULTIPLIER
        alpha = 0.10 if ambient > self.noise_floor else 0.01
        drifted = (1 - alpha) * self.noise_floor + alpha * ambient
        self.noise_floor = max(config.VAD_THRESHOLD, min(drifted, config.VAD_MAX_FLOOR))

    def listen(
        self,
        max_wait_s: float | None = None,
        should_stop=None,
    ) -> Utterance | None:
        """Wait for speech, record until it stops, return the audio.

        Returns None on timeout, on a caller-requested stop, or when the
        speech was too short to be anything but a cough.
        """
        if self._stream is None:
            raise AudioError("Microphone is not open")
        # Start from live audio, not whatever accumulated while E.V. was busy.
        self.flush()

        preroll_frames = max(1, config.PREROLL_MS // config.FRAME_MS)
        min_speech_frames = max(1, config.MIN_SPEECH_MS // config.FRAME_MS)
        hang_frames = max(1, config.SILENCE_HANG_MS // config.FRAME_MS)
        max_frames = int(config.MAX_UTTERANCE_S * 1000 / config.FRAME_MS)

        preroll: list[bytes] = []
        collected: list[bytes] = []
        speech_frames = 0
        silence_frames = 0
        triggered = False
        deadline = time.monotonic() + max_wait_s if max_wait_s else None

        while True:
            if should_stop is not None and should_stop():
                return None
            if not triggered and deadline is not None and time.monotonic() > deadline:
                return None

            item = self.next_frame(timeout=0.5)
            if item is None:
                if not self._reading:
                    return None
                continue
            frame, level = item
            is_speech = level > self.noise_floor

            if not triggered:
                preroll.append(frame)
                if len(preroll) > preroll_frames:
                    preroll.pop(0)
                if is_speech:
                    speech_frames += 1
                    if speech_frames >= min_speech_frames:
                        triggered = True
                        # Keep the pre-roll so the first syllable is not clipped.
                        collected = list(preroll)
                        silence_frames = 0
                else:
                    speech_frames = 0
                    # Track the room while idle. A fan spinning up or a window
                    # opening would otherwise leave the threshold stale and
                    # either deafen E.V. or make it trigger on nothing.
                    self._drift_noise_floor(level)
                continue

            collected.append(frame)
            if is_speech:
                silence_frames = 0
            else:
                silence_frames += 1
                if silence_frames >= hang_frames:
                    break

            if len(collected) >= max_frames:
                log.info("Hit the max utterance length; cutting it off")
                break

        pcm = b"".join(collected)
        duration = len(pcm) / 2 / self.sample_rate
        if duration < config.MIN_SPEECH_MS / 1000:
            return None
        return Utterance(pcm, duration, self.sample_rate)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


class _MciPlayer:
    """MP3 playback through Windows' built-in MCI, via ctypes.

    Each clip gets a unique alias so a stale handle from a barged-in clip can
    never silence the next one.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._winmm = ctypes.WinDLL("winmm")
        # Declaring the signature matters on 64-bit: without it ctypes guesses
        # at the pointer arguments and `status` can return junk.
        self._winmm.mciSendStringW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.UINT,
            wintypes.HWND,
        ]
        self._winmm.mciSendStringW.restype = ctypes.c_uint
        self._ctypes = ctypes
        self._alias: str | None = None
        self._counter = 0
        self._lock = threading.Lock()

    def _send(self, command: str) -> tuple[int, str]:
        buffer = self._ctypes.create_unicode_buffer(512)
        code = self._winmm.mciSendStringW(command, buffer, 511, None)
        return code, buffer.value

    def _error(self, code: int) -> str:
        buffer = self._ctypes.create_unicode_buffer(256)
        try:
            self._winmm.mciGetErrorStringW(code, buffer, 255)
        except Exception:
            return f"MCI error {code}"
        return buffer.value or f"MCI error {code}"

    def play(self, path: str, block: bool = True, timeout: float = 30.0) -> bool:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            log.warning("Refusing to play a missing or empty file: %s", path)
            return False

        with self._lock:
            self._counter += 1
            alias = f"evtts{self._counter}"
            # `type mpegvideo` is what MCI calls its MP3/media decoder.
            code, _ = self._send(f'open "{path}" type mpegvideo alias {alias}')
            if code != 0:
                code, _ = self._send(f'open "{path}" alias {alias}')
                if code != 0:
                    log.warning("MCI could not open %s: %s", path, self._error(code))
                    return False
            self._alias = alias
            code, _ = self._send(f"play {alias}")
            if code != 0:
                log.warning("MCI could not play %s: %s", path, self._error(code))
                self._send(f"close {alias}")
                self._alias = None
                return False

        if not block:
            return True

        # MCI reports "playing" a moment after `play` returns. Polling straight
        # away can observe "stopped" and cut the clip off before a word of it
        # is heard, so wait for the transition before treating it as the end.
        started = self._await_state(alias, "playing", grace=0.5)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._alias != alias:
                    return True  # stopped by a barge-in
                _, mode = self._send(f"status {alias} mode")
            if started and mode != "playing":
                break
            if not started and mode in {"stopped", ""}:
                break
            time.sleep(0.04)

        with self._lock:
            self._send(f"close {alias}")
            if self._alias == alias:
                self._alias = None
        return True

    def _await_state(self, alias: str, want: str, grace: float) -> bool:
        """Wait up to `grace` seconds for the device to reach `want`."""
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self._lock:
                if self._alias != alias:
                    return False
                _, mode = self._send(f"status {alias} mode")
            if mode == want:
                return True
            time.sleep(0.01)
        return False

    def warm(self, path: str) -> None:
        """Load the MP3 decoder once, without playing anything.

        The first `open` on a cold MCI costs ~390ms while Windows loads the
        codec; every one after that is ~20ms. Paying it during startup keeps
        it off the user's first reply.
        """
        try:
            code, _ = self._send(f'open "{path}" type mpegvideo alias evwarm')
            if code == 0:
                self._send("close evwarm")
        except Exception as exc:
            log.debug("MCI warm failed: %s", exc)

    def stop(self) -> None:
        with self._lock:
            if self._alias:
                self._send(f"stop {self._alias}")
                self._send(f"close {self._alias}")
                self._alias = None


class _SubprocessPlayer:
    """Fallback for non-Windows hosts, or if MCI is unavailable."""

    def __init__(self, argv_template: list[str]) -> None:
        self._template = argv_template
        self._process = None
        self._lock = threading.Lock()

    def play(self, path: str, block: bool = True, timeout: float = 30.0) -> bool:
        import subprocess

        argv = [path if part == "{}" else part for part in self._template]
        with self._lock:
            try:
                self._process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                log.warning("Playback process failed: %s", exc)
                return False
            process = self._process
        if block:
            try:
                process.wait(timeout=timeout)
            except Exception:
                process.kill()
        return True

    def stop(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.kill()
            self._process = None


def build_player():
    """Pick the lightest playback backend this machine supports."""
    if IS_WINDOWS:
        try:
            return _MciPlayer()
        except Exception as exc:
            log.debug("MCI unavailable: %s", exc)

    from tools.base import resolve_executable

    for binary, argv in (
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{}"]),
        ("mpv", ["mpv", "--no-video", "--really-quiet", "{}"]),
        ("afplay", ["afplay", "{}"]),
        ("mpg123", ["mpg123", "-q", "{}"]),
    ):
        if resolve_executable(binary):
            return _SubprocessPlayer(argv)

    raise AudioError(
        "No audio playback backend. Install ffmpeg (for ffplay) or mpv, "
        "or set EV_TTS_ENABLED=false."
    )
