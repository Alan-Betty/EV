"""E.V. - Everyday Virtual Assistant. Main async event loop.

    listen -> transcribe -> (local intent?) -> decide -> act -> speak

Every blocking piece (microphone reads, subprocess launches, keystroke
automation) is pushed onto a worker thread, so a slow tool never stalls the
loop and Ctrl+C always lands.

Two things make this feel like an assistant rather than a request/response
toy:

* **Control phrases are matched locally.** "E.V., take five" never touches the
  network, so it lands instantly - including while E.V. is mid-sentence.
* **The microphone never stops.** A background reader thread keeps capturing
  while E.V. talks, so the user can talk over it.

Run it:
    python ev_core.py             # voice
    python ev_core.py --text      # typed input, same brain and tools
    python ev_core.py --say "hi"  # one-shot command, no loop
    python ev_core.py --check     # verify configuration and exit
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

import httpx

import config
from ev import wake
from ev.audio import AudioError, Microphone
from ev.brain import Brain, BrainError, ToolCall
from ev.session import (
    Intent,
    Session,
    all_responses,
    match_intent,
    response_for,
)
from ev.stt import Transcriber, TranscriptionError
from ev.tts import Speaker, clean_for_speech
from tools import ToolResult, dispatch
from tools.safety import is_affirmative

log = logging.getLogger("ev")

BANNER = """
  ============================================
    E.V.  -  Everyday Virtual Assistant
    cloud brain, local hands
    {provider}
    voice: {voice}
  ============================================
"""


class EV:
    """Owns the session: audio devices, network clients, and history."""

    def __init__(self, text_mode: bool = False) -> None:
        self.text_mode = text_mode or config.TEXT_MODE
        # One HTTP client for both the brain and STT: connection reuse cuts a
        # full TLS handshake off every single utterance.
        self._http = httpx.AsyncClient(
            timeout=config.LLM_TIMEOUT_S,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self.brain = Brain(self._http)
        self.transcriber = Transcriber(self._http)
        self.speaker = Speaker()
        self.session = Session()
        self.mic: Microphone | None = None
        self._running = False

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        # Warm the TTS stack while the microphone calibrates, so neither cost
        # lands on the user's first command.
        warmup = asyncio.create_task(self.speaker.warmup())
        # Confirm the model exists before the user starts talking to it, not
        # on every command afterwards.
        verify = asyncio.create_task(self.brain.verify_model())
        try:
            if not self.text_mode:
                self.mic = Microphone()
                await asyncio.to_thread(self.mic.open)
                print("Calibrating to room noise, stay quiet for a second...")
                await asyncio.to_thread(self.mic.calibrate)
        finally:
            await warmup
            try:
                await verify
            except BrainError as exc:
                # A bad model name is fatal, but the message tells the user
                # exactly which models they can use instead.
                print(f"\n{exc}\n", file=sys.stderr)
                raise
        self._running = True

        # Cache the stock replies in the background. "Standing by." should not
        # cost a network round trip when the intent behind it costs nothing.
        if not self.text_mode:
            asyncio.create_task(self.speaker.prewarm(all_responses() + ["Yeah?"]))

    async def stop(self) -> None:
        self._running = False
        self.speaker.stop()
        if self.mic is not None:
            await asyncio.to_thread(self.mic.close)
            self.mic = None
        await self._http.aclose()

    def request_stop(self) -> None:
        self._running = False
        self.speaker.stop()

    # -- input ------------------------------------------------------------
    async def _next_utterance(self, wait_s: float | None) -> str:
        """One transcript from the microphone, or an empty string."""
        if self.mic is None:
            return ""
        utterance = await asyncio.to_thread(
            self.mic.listen, wait_s, lambda: not self._running
        )
        if utterance is None:
            return ""
        try:
            return await self.transcriber.transcribe(utterance.wav)
        except TranscriptionError as exc:
            log.warning("Transcription failed: %s", exc)
            print(f"[E.V.] Couldn't transcribe that: {exc}")
            return ""

    async def _next_typed(self) -> str:
        prompt = "standby > " if self.session.in_standby else "you > "
        try:
            return (await asyncio.to_thread(input, prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            self.request_stop()
            return ""

    # -- output -----------------------------------------------------------
    async def say(self, text: str) -> None:
        """Print and speak a reply, interruptible by the user talking over it."""
        spoken = clean_for_speech(text)
        if not spoken:
            return
        print(f"E.V. > {spoken}")

        monitor = None
        if self.mic is not None and config.TTS_BARGE_IN:
            monitor = asyncio.create_task(self._watch_for_barge_in())
        try:
            await self.speaker.say(spoken)
        finally:
            if monitor is not None:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
        self.session.mark_replied()

    async def _watch_for_barge_in(self) -> None:
        """Cut playback the moment the user starts talking over E.V.

        Deliberately conservative. On speakers rather than headphones, E.V.
        hears itself, so this waits for speech that is both sustained and
        louder than its own tail before giving up the floor.
        """
        if self.mic is None:
            return
        await asyncio.sleep(0.6)  # ignore the attack of E.V.'s own first word
        while self.speaker.speaking:
            if self.mic.speech_energy() >= config.BARGE_IN_FRAMES:
                log.info("Barge-in detected; stopping playback")
                self.speaker.stop()
                return
            await asyncio.sleep(0.05)

    # -- the loop ---------------------------------------------------------
    async def run(self) -> None:
        await self.start()
        model = (
            config.GROQ_MODEL if config.LLM_PROVIDER == "groq" else config.GEMINI_MODEL
        )
        print(BANNER.format(provider=f"{config.LLM_PROVIDER}: {model}", voice=config.TTS_VOICE))

        if self.text_mode:
            print("Text mode. Type a command, or 'quit' to exit.\n")
        else:
            hint = (
                f"Say '{config.WAKE_PHRASES[0]}' to wake me."
                if config.WAKE_REQUIRED
                else "Wake phrase off - just talk."
            )
            print(f"Listening. {hint} Ctrl+C to quit.\n")

        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                self.request_stop()
            except Exception:
                log.exception("Unhandled error in the main loop")
                await self.say("Something broke on my end. Still here.")

    async def _tick(self) -> None:
        """One pass: get input, route it, act on it."""
        transcript = (
            await self._next_typed()
            if self.text_mode
            else await self._next_utterance(
                wait_s=1.0 if self.session.in_followup_window() else None
            )
        )
        if not transcript:
            return

        if not self.text_mode:
            print(f"you  > {transcript}")

        if self.text_mode and transcript.lower() in {"quit", "exit"}:
            self.request_stop()
            return

        command = self._extract_command(transcript)
        if command is None:
            return

        # Control phrases resolve locally, with no network round trip.
        intent = match_intent(command or transcript, self.session.mode)
        if intent is not None:
            await self._handle_intent(intent)
            return

        if self.session.in_standby:
            return  # asleep; that was not for us

        if not command:
            # Woken with nothing else said: acknowledge, then wait for it.
            await self.say("Yeah?")
            command = await self._next_utterance(wait_s=6.0)
            if not command:
                return
            print(f"you  > {command}")
            follow_up = match_intent(command, self.session.mode)
            if follow_up is not None:
                await self._handle_intent(follow_up)
                return

        await self.handle(command)

    def _extract_command(self, transcript: str) -> str | None:
        """Strip the wake phrase. None means "this was not addressed to E.V."."""
        if self.text_mode:
            return transcript

        if self.session.in_standby:
            # Asleep: a wake phrase is welcome but not required, because
            # "E.V., wake up" and a bare "wake up" mean the same thing.
            match = wake.detect(transcript)
            return match.command if match.matched and match.command else transcript

        if self.session.in_followup_window():
            match = wake.detect(transcript)
            return match.command if match.matched else transcript

        match = wake.detect(transcript)
        if match.matched:
            return match.command

        # Near-misses are reported rather than ignored. Silence here is what
        # makes a voice assistant feel dead.
        if wake.heard_something_like_a_name(transcript):
            print(f"[E.V.] Heard something close to my name but wasn't sure. "
                  f"Try starting with '{config.WAKE_PHRASES[0]}'.")
        else:
            log.debug("No wake phrase in %r", transcript)
        return None

    async def _handle_intent(self, intent: Intent) -> None:
        """Act on a locally matched control phrase."""
        self.speaker.stop()  # whatever E.V. was saying, it is over now

        if intent is Intent.STANDBY:
            self.session.enter_standby()
            await self.say(response_for(intent))
            print("[E.V.] Standing by. Say 'wake up' or 'E.V., wake up' to resume.")
            return

        if intent is Intent.RESUME:
            self.session.resume()
            await self.say(response_for(intent))
            return

        if intent is Intent.CANCEL:
            self.session.pending = None
            await self.say(response_for(intent))
            return

        if intent is Intent.SHUTDOWN:
            await self.say(response_for(intent))
            self.request_stop()
            return

        await self.say(response_for(intent))

    async def handle(self, command: str) -> None:
        """Route one command: confirmation reply, or a fresh model turn."""
        if self.session.pending is not None:
            await self._resolve_pending(command)
            return

        try:
            call = await self.brain.decide(command)
        except BrainError as exc:
            log.warning("Brain error: %s", exc)
            await self.say(str(exc))
            return

        log.info("tool=%s args=%s", call.name, call.arguments)
        await self._execute(command, call)

    async def _execute(self, command: str, call: ToolCall) -> None:
        # Tools block on subprocesses and the OS, so they run off the loop.
        result: ToolResult = await asyncio.to_thread(dispatch, call.name, call.arguments)

        if result.needs_confirmation:
            self.session.pending = {"command": command, "args": dict(result.data)}
            await self.say(result.speech)
            if not self.text_mode:
                # Confirmation has a short fuse; silence means no.
                reply = await self._next_utterance(wait_s=8.0)
                if reply:
                    print(f"you  > {reply}")
                await self._resolve_pending(reply)
            return

        await self.say(result.speech)
        self.brain.remember(command, result.detail or result.speech)

    async def _resolve_pending(self, reply: str) -> None:
        pending = self.session.pending
        self.session.pending = None
        if pending is None:
            return

        if not reply or not is_affirmative(reply):
            await self.say("Cancelled.")
            self.brain.remember(
                pending["command"], "The user declined to confirm; the command was not run."
            )
            return

        args = {**pending["args"], "confirmed": True}
        args.pop("reason", None)
        result: ToolResult = await asyncio.to_thread(dispatch, "terminal_command", args)
        await self.say(result.speech)
        self.brain.remember(pending["command"], result.detail or result.speech)

    # -- one-shot ---------------------------------------------------------
    async def run_once(self, command: str) -> None:
        self._running = True
        try:
            intent = match_intent(command, self.session.mode)
            if intent is not None:
                await self._handle_intent(intent)
            else:
                await self.handle(command)
        finally:
            self._running = False


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # httpx logs every request at INFO, which drowns out everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def check_config() -> int:
    """Print a readiness report. Returns a shell exit code."""
    import importlib.util
    import shutil

    problems: list[str] = []
    notes: list[str] = []

    provider = config.LLM_PROVIDER
    key = config.GROQ_API_KEY if provider == "groq" else config.GEMINI_API_KEY
    key_name = "GROQ_API_KEY" if provider == "groq" else "GEMINI_API_KEY"
    if key:
        notes.append(f"OK   brain: {provider} ({key_name} set, {len(key)} chars)")
        if provider == "groq":
            # A model that 404s is the single most confusing failure mode:
            # every command errors and it looks like a bad key.
            try:
                import httpx as _httpx

                response = _httpx.get(
                    f"{config.GROQ_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10.0,
                )
                if response.status_code == 200:
                    available = {item["id"] for item in response.json().get("data", [])}
                    if config.GROQ_MODEL in available:
                        notes.append(f"OK   model: {config.GROQ_MODEL}")
                    else:
                        alternative = next(
                            (m for m in config.GROQ_MODEL_FALLBACKS if m in available),
                            None,
                        )
                        if alternative:
                            notes.append(
                                f"WARN model: '{config.GROQ_MODEL}' unavailable; "
                                f"will fall back to '{alternative}'"
                            )
                        else:
                            usable = sorted(
                                n for n in available
                                if not any(s in n for s in ("whisper", "guard", "orpheus"))
                            )
                            problems.append(
                                f"MISS model: '{config.GROQ_MODEL}' unavailable and no "
                                f"fallback matched. Set EV_GROQ_MODEL to one of: "
                                f"{', '.join(usable) or 'none'}"
                            )
                elif response.status_code == 401:
                    problems.append("MISS brain: Groq rejected the API key")
            except Exception as exc:
                notes.append(f"WARN model: could not verify ({exc})")
    else:
        problems.append(f"MISS brain: {key_name} is not set")

    if config.STT_PROVIDER == "groq" and not config.GROQ_API_KEY:
        problems.append("MISS stt: groq backend needs GROQ_API_KEY")
    else:
        notes.append(f"OK   stt: {config.STT_PROVIDER} ({config.GROQ_STT_MODEL})")

    for module, label, required in (
        ("edge_tts", "edge-tts (voice output)", config.TTS_ENABLED),
        ("sounddevice", "sounddevice (microphone)", True),
        ("pyautogui", "pyautogui (dev_workflow keystrokes)", False),
        ("httpx", "httpx (API calls)", True),
    ):
        if importlib.util.find_spec(module) is not None:
            notes.append(f"OK   {label}")
        elif required:
            problems.append(f"MISS {label} - pip install -r requirements.txt")
        else:
            notes.append(f"WARN {label} not installed (optional)")

    # Naming a working microphone is far more useful than asserting one exists.
    try:
        import sounddevice as sd

        inputs = [
            (index, device["name"])
            for index, device in enumerate(sd.query_devices())
            if device.get("max_input_channels", 0) > 0
        ]
        if inputs:
            notes.append(f"OK   microphone: {len(inputs)} input(s), e.g. [{inputs[0][0]}] {inputs[0][1][:40]}")
        else:
            problems.append("MISS microphone: no input devices found")
    except Exception as exc:
        notes.append(f"WARN microphone: could not enumerate devices ({exc})")

    for binary, label in (
        (config.VSCODE_CLI, "VS Code CLI"),
        (config.CLAUDE_CLI, "Claude Code CLI"),
    ):
        if shutil.which(binary):
            notes.append(f"OK   {label} ({binary})")
        else:
            notes.append(f"WARN {label} '{binary}' not on PATH - dev_workflow degrades")

    print("\nE.V. configuration check\n" + "-" * 44)
    for line in notes:
        print(line)
    for line in problems:
        print(line)
    print("-" * 44)
    print("Ready.\n" if not problems else f"{len(problems)} problem(s) to fix.\n")
    return 1 if problems else 0


async def _amain(args: argparse.Namespace) -> int:
    try:
        assistant = EV(text_mode=args.text)
    except BrainError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 2

    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, AttributeError):
        # Windows ProactorEventLoop has no signal handlers; KeyboardInterrupt
        # in the main loop covers it there.
        loop.add_signal_handler(signal.SIGINT, assistant.request_stop)

    try:
        if args.say:
            await assistant.run_once(args.say)
        else:
            await assistant.run()
    except AudioError as exc:
        print(f"\nAudio problem: {exc}", file=sys.stderr)
        print("Tip: run with --text to skip the microphone entirely.", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        pass
    finally:
        await assistant.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ev", description="E.V. - Everyday Virtual Assistant"
    )
    parser.add_argument("--text", action="store_true", help="type instead of speaking")
    parser.add_argument("--say", metavar="COMMAND", help="run one command and exit")
    parser.add_argument("--check", action="store_true", help="check setup and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    _configure_logging(args.verbose)

    if args.check:
        return check_config()

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nShutting down.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
