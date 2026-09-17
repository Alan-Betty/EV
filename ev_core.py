"""E.V. - Everyday Virtual Assistant. Main async event loop.

    listen -> transcribe -> (local intent?) -> decide -> act -> speak

Every blocking piece (microphone reads, subprocess launches, keystroke
automation) is pushed onto a worker thread, so a slow tool never stalls the
loop and Ctrl+C always lands.

Three things make this feel like an assistant rather than a request/response
toy:

* **Control phrases are matched locally.** "E.V., take five" never touches the
  network, so it lands instantly - including while E.V. is mid-sentence.
* **The microphone never stops.** A background reader thread keeps capturing
  while E.V. talks, so the user can talk over it.
* **Speech starts mid-generation.** A `chat` reply is streamed out of the
  model and spoken a sentence at a time, so the gap between the user finishing
  and E.V. starting is the time to generate one sentence, not a whole reply.

The terminal UI lives entirely in `ev.ui` and is a strict dead end: it renders
labels, panels and spinners, and none of it can reach `Speaker.say`, which is
handed the reply text on a separate path. That is what keeps E.V. from reading
its own chrome aloud.

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
import random
import signal
import sys
import threading

import httpx

import config
from ev import wake
from ev.audio import AudioError, Microphone
from ev.backlog import get_backlog
from ev.brain import Brain, BrainError, ToolCall
from ev.memory import StartupReport, get_memory
from ev.session import (
    Intent,
    Session,
    all_responses,
    is_resume_phrase,
    match_intent,
    response_for,
)
from ev.stt import Transcriber, TranscriptionError
from ev.tts import Speaker, SpeechStream, clean_for_speech
from ev.ui import UI
from tools import CANCELLABLE, CancelToken, ToolResult, dispatch
from tools.safety import is_affirmative, is_negative

log = logging.getLogger("ev")


def _describe(call: ToolCall) -> str:
    """One-line summary of a tool call, for the terminal only."""
    interesting = ("app", "query", "url", "action", "path", "command", "directory")
    parts = [
        f"{key}={value}"
        for key, value in call.arguments.items()
        if key in interesting and str(value).strip()
    ]
    return "  ".join(parts)[:100]


# Handed to the model when Whisper flagged the transcript as unclear. It is
# advice, not an instruction to stop: most fuzzy transcripts are perfectly
# actionable, and refusing every one of them would be worse than the guessing.
_UNCLEAR_AUDIO = (
    "The speech recognition was unsure about that transcript, so some words "
    "may be wrong. If the request is clear enough to act on, act on it. If a "
    "misheard word would make you do the wrong thing, use chat to ask the "
    "user to repeat it instead of guessing."
)


def _acknowledgement() -> str:
    """A short "still here, working on it" line. Plain speech, no labels."""
    return random.choice(config.ACK_PHRASES) if config.ACK_PHRASES else "On it."


def _summarise_call(call: ToolCall) -> str:
    """How a backlog entry should read when it is handed back tomorrow."""
    described = _describe(call)
    return f"{call.name}: {described}" if described else call.name


class EV:
    """Owns the session: audio devices, network clients, and history."""

    def __init__(self, text_mode: bool = False) -> None:
        self.text_mode = text_mode or config.TEXT_MODE
        self.ui = UI()
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
        # Both survive a reboot. `memory` carries preferences and how long E.V.
        # was off; `backlog` carries what the last session never finished.
        self.memory = get_memory()
        self.backlog = get_backlog()
        self.mic: Microphone | None = None
        self._running = False
        self._boot_report: StartupReport | None = None
        # Something the user said over a running tool that was not a cancel.
        # Held rather than dropped: it is almost always the next command.
        self._queued_utterance: str | None = None
        # Set when "stop" was heard for a tool that cannot honour it.
        self._cancel_refused: str | None = None

    # -- lifecycle --------------------------------------------------------
    def _boot(self) -> StartupReport:
        """Open the persisted session and hand the model what it should know.

        Idempotent, because `--say` reaches the loop by a different route than
        the interactive run and both need the state loaded.

        The reads are two small JSON files, so this stays on the event loop
        rather than going through `asyncio.to_thread`: the thread hop would
        cost more than the read.
        """
        if self._boot_report is not None:
            return self._boot_report

        report = self.memory.begin_session()
        self._boot_report = report

        # One standing block, assembled once. It is in the system prompt on
        # every turn, so each piece has to earn its tokens.
        pieces = [report.context, self.memory.context(), self.backlog.context()]
        self.brain.session_context = " ".join(piece for piece in pieces if piece)

        self.transcriber.set_hints(self._stt_hints())
        return report

    def _stt_hints(self) -> list[str]:
        """Proper nouns this machine is likely to hear, for the STT prompt.

        Ordered by how badly the recogniser mangles them without help.
        Program names are the worst offenders - they are proper nouns that
        sound like ordinary words, so "open Brave" becomes "open brave" and
        "run OBS" becomes "run obese". The curated aliases come first because
        they are the names people actually say; the indexed Start Menu entries
        follow, shortest first, since a long shortcut name is rarely spoken
        aloud in full.
        """
        from tools.app_launcher import app_index

        words = [name.title() for name in config.APP_ALIASES]
        words += sorted(
            (name.title() for name in app_index().apps()), key=lambda n: (len(n), n)
        )
        words += [name.title() for name in config.USER_DIRS]
        words += [item.text for item in self.backlog.pending()[:5]]
        return words

    async def _report_state(self) -> None:
        """Say what carried over from last time, if anything did.

        Spoken, because the whole point of a backlog is being told about it
        rather than having to go and look. Silent on a clean, recent restart -
        being greeted every time you restart a process gets old fast.
        """
        report = self._boot_report
        if report is None:
            return

        greeting = report.greeting
        if greeting:
            await self.say(greeting)

        summary = self.backlog.summary()
        if summary:
            await self.say(summary)
            self.ui.note(self.backlog.listing())

    async def start(self) -> None:
        # Warm the TTS stack while the microphone calibrates, so neither cost
        # lands on the user's first command.
        warmup = asyncio.create_task(self.speaker.warmup())
        # Confirm the model exists before the user starts talking to it, not
        # on every command afterwards.
        verify = asyncio.create_task(self.brain.verify_model())
        self._boot()
        try:
            if not self.text_mode:
                self.mic = Microphone()
                await asyncio.to_thread(self.mic.open)
                with self.ui.status("Calibrating to room noise..."):
                    await asyncio.to_thread(self.mic.calibrate)
        finally:
            await warmup
            try:
                await verify
            except BrainError as exc:
                # A bad model name is fatal, but the message tells the user
                # exactly which models they can use instead.
                self.ui.error(str(exc))
                raise
        self._running = True

        # Cache the stock replies in the background. "Standing by." should not
        # cost a network round trip when the intent behind it costs nothing.
        if not self.text_mode:
            # The acknowledgements are in here for the same reason: "On it,
            # stand by." has to land immediately or it is not an
            # acknowledgement, it is an interruption.
            asyncio.create_task(
                self.speaker.prewarm(
                    all_responses() + ["Yeah?"] + list(config.ACK_PHRASES)
                )
            )

    async def stop(self) -> None:
        self._running = False
        self.speaker.stop()
        if self.mic is not None:
            await asyncio.to_thread(self.mic.close)
            self.mic = None
        # Marks the shutdown clean, which is what keeps the next start from
        # reporting a crash that did not happen.
        self.memory.end_session(clean=True)
        await self._http.aclose()

    def request_stop(self) -> None:
        self._running = False
        self.speaker.stop()

    # -- input ------------------------------------------------------------
    async def _next_utterance(self, wait_s: float | None) -> str:
        """One transcript from the microphone, or an empty string."""
        if self.mic is None:
            return ""

        # The spinner is only shown on the blocking idle wait. While a
        # conversation is open this polls every couple of seconds, and a
        # spinner that tears down and rebuilds that often just flickers.
        if wait_s is None:
            with self.ui.status("Listening..."):
                utterance = await asyncio.to_thread(
                    self.mic.listen, wait_s, lambda: not self._running
                )
        else:
            utterance = await asyncio.to_thread(
                self.mic.listen, wait_s, lambda: not self._running
            )
        if utterance is None:
            return ""

        # In standby the only thing worth hearing is a couple of words. A
        # cough or a passing sentence is not, and transcribing it costs a Groq
        # call for something that is going to be dropped anyway.
        if self.session.in_standby and utterance.duration_s > config.STANDBY_MAX_UTTERANCE_S:
            log.debug("Ignoring %.1fs of room noise while in standby", utterance.duration_s)
            return ""

        try:
            with self.ui.status("Transcribing..."):
                transcript = await self.transcriber.transcribe(utterance.wav)
            # Feeds the next utterance's decoding prompt: names and jargon
            # carry across a conversation, and Whisper reads the prompt as
            # text immediately preceding the audio.
            if transcript and not transcript.rejected:
                self.transcriber.note_transcript(transcript)
            return transcript
        except TranscriptionError as exc:
            log.warning("Transcription failed: %s", exc)
            self.ui.warn(f"Couldn't transcribe that: {exc}")
            return ""

    async def _next_typed(self) -> str:
        prompt = self.ui.prompt(self.session.engaged, self.session.in_standby)
        try:
            return (await asyncio.to_thread(input, prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            self.request_stop()
            return ""

    # -- output -----------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _barge_in(self):
        """Watch for the user talking over E.V. for the duration of a block."""
        monitor = None
        if self.mic is not None and config.TTS_BARGE_IN:
            monitor = asyncio.create_task(self._watch_for_barge_in())
        try:
            yield
        finally:
            if monitor is not None:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor

    async def say(self, text: str) -> None:
        """Print and speak a reply, interruptible by the user talking over it.

        `spoken` is computed once and used for both the panel and the speaker.
        The UI is handed the finished string and adds its decoration on its own
        side, so no label the terminal draws can ever reach synthesis.
        """
        spoken = clean_for_speech(text)
        if not spoken:
            return
        self.ui.speech(spoken)

        async with self._barge_in():
            await self.speaker.say(spoken)

        # Replying keeps the conversation open, so the user's next sentence
        # needs no wake phrase.
        self.session.mark_exchange()
        # Throttled internally, so this is a no-op on most turns. It bounds
        # how much of a session a power cut can erase.
        self.memory.touch()

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
        self.ui.header(
            provider=f"{config.LLM_PROVIDER}  {model}",
            voice=config.TTS_VOICE,
            mode="text" if self.text_mode else "voice",
        )

        if self.text_mode:
            self.ui.hint("Text mode. Type a command, or 'quit' to exit.\n")
        else:
            hint = (
                f"Say '{config.WAKE_PHRASES[0]}' to start."
                if config.WAKE_REQUIRED
                else "Wake phrase off - just talk."
            )
            self.ui.hint(f"{hint}  Ctrl+C to quit.")
            self.ui.hint(
                f"Once we're talking you can drop the name for "
                f"{int(config.CONVERSATION_WINDOW_S)}s. Say 'take five' to pause me.\n"
            )

        # Brand new day: how long we were down, and what is still open.
        await self._report_state()

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
        # Something said over a running tool that turned out not to be a
        # cancel. It was already heard and already echoed, so it skips the
        # microphone entirely rather than making the user repeat themselves.
        if self._queued_utterance is not None:
            queued, self._queued_utterance = self._queued_utterance, None
            command = self._extract_command(queued)
            if command is not None:
                await self._route(queued, command)
            return

        transcript = (
            await self._next_typed()
            if self.text_mode
            else await self._next_utterance(
                # While engaged, poll on a short timeout so the conversation
                # window can expire; when idle, just block until someone talks.
                wait_s=2.0 if self.session.engaged else None
            )
        )
        if not transcript:
            return

        if not self.text_mode:
            self.ui.user(transcript, engaged=self.session.engaged)

        if self.text_mode and transcript.lower() in {"quit", "exit"}:
            self.request_stop()
            return

        command = self._extract_command(transcript)
        if command is None:
            return

        await self._route(transcript, command)

    async def _route(self, transcript: str, command: str) -> None:
        """Decide what one addressed utterance means, and act on it.

        Split out of `_tick` so an utterance heard *during* a running tool can
        take exactly the same path afterwards. Anything held that way has
        already been transcribed and echoed; re-listening for it would make
        the user say it twice.
        """
        # Checked only once E.V. knows it was being spoken to. A rejected
        # transcript from across the room should stay silent, exactly like any
        # other utterance that was not addressed here.
        if getattr(transcript, "rejected", False):
            self.ui.warn(f"Didn't catch that clearly ({transcript.why()}).")
            await self.say("Didn't catch that. Say it again?")
            return

        # Control phrases resolve locally, with no network round trip.
        intent = match_intent(command or transcript, self.session.mode)
        if intent is not None:
            await self._handle_intent(intent)
            return

        if self.session.in_standby:
            # `match_intent` above is exact, so "hey, wake up" and "EV, you
            # awake?" both fall through it. A second, looser pass catches
            # them: standby has no real commands, so nothing can be swallowed.
            if is_resume_phrase(command or transcript):
                await self._handle_intent(Intent.RESUME)
                return
            # Drawn, never spoken - answering aloud would defeat standby. But
            # echoing the user's words and then saying nothing at all is what
            # makes this look broken rather than asleep.
            self.ui.note(
                f"Still in standby. Say 'wake up', or "
                f"'{config.WAKE_PHRASES[0]}, wake up', to bring me back."
            )
            return  # asleep; that was not for us

        if not command:
            # Woken with nothing else said: acknowledge, then wait for it.
            await self.say("Yeah?")
            command = await self._next_utterance(wait_s=6.0)
            if not command:
                return
            self.ui.user(command, engaged=True)
            follow_up = match_intent(command, self.session.mode)
            if follow_up is not None:
                await self._handle_intent(follow_up)
                return

        await self.handle(command, uncertain=getattr(transcript, "uncertain", False))

    def _extract_command(self, transcript: str) -> str | None:
        """Strip the wake phrase. None means "this was not addressed to E.V."."""
        if self.text_mode:
            return transcript

        match = wake.detect(transcript)

        if self.session.in_standby:
            # Asleep: a wake phrase is welcome but not required, because
            # "E.V., wake up" and a bare "wake up" mean the same thing.
            return match.command if match.matched and match.command else transcript

        # Mid-conversation the name is optional. This is the whole point of
        # the engaged state: saying "E.V." before every sentence is exhausting.
        if self.session.engaged:
            return match.command if match.matched else transcript

        if match.matched:
            self.session.engage()
            return match.command

        # Near-misses are reported rather than ignored. Silence here is what
        # makes a voice assistant feel dead.
        if wake.heard_something_like_a_name(transcript):
            self.ui.note(
                f"Heard something close to my name but wasn't sure. "
                f"Try starting with '{config.WAKE_PHRASES[0]}'."
            )
        else:
            log.debug("No wake phrase in %r", transcript)
        return None

    async def _handle_intent(self, intent: Intent) -> None:
        """Act on a locally matched control phrase."""
        self.speaker.stop()  # whatever E.V. was saying, it is over now

        if intent is Intent.STANDBY:
            self.session.enter_standby()
            await self.say(response_for(intent))
            self.ui.note("Standing by. Say 'wake up' or 'E.V., wake up' to resume.")
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

    # -- acting -----------------------------------------------------------
    @property
    def _streaming(self) -> bool:
        """Streamed speech is only worth the machinery when there is audio."""
        return config.LLM_STREAMING and self.speaker.enabled

    async def handle(self, command: str, uncertain: bool = False) -> None:
        """Route one command: confirmation reply, or a fresh model turn.

        `uncertain` means Whisper was not confident in the transcript. It is
        passed to the model as context rather than acted on here: E.V. cannot
        tell whether a fuzzy transcript is ambiguous, but the model reading it
        alongside the request can, and can ask instead of guessing.
        """
        if self.session.pending is not None:
            await self._resolve_pending(command)
            return

        # A `chat` reply starts playing while the model is still writing it.
        # The stream is created on the first sentence rather than up front, so
        # a tool call never leaves an idle stream holding the speaker lock.
        stream: SpeechStream | None = None

        def on_sentence(sentence: str) -> None:
            nonlocal stream
            if stream is None:
                stream = self.speaker.stream()
            stream.feed(sentence)

        try:
            with self.ui.status("Thinking..."):
                call = await self.brain.decide(
                    command,
                    extra_context=_UNCLEAR_AUDIO if uncertain else "",
                    on_sentence=on_sentence if self._streaming else None,
                )
        except BrainError as exc:
            if stream is not None:
                stream.cancel()
            log.warning("Brain error: %s", exc)
            await self.say(str(exc))
            return

        log.info("tool=%s args=%s", call.name, call.arguments)
        self.ui.action(call.name, _describe(call))

        if stream is not None:
            if call.name == "chat":
                await self._finish_streamed(command, call, stream)
                return
            # Only `chat` streams, so this should not happen - but an
            # abandoned stream would hold the speaker lock forever.
            stream.cancel()

        await self._execute(command, call)

    async def _finish_streamed(
        self, command: str, call: ToolCall, stream: SpeechStream
    ) -> None:
        """Show the reply and wait for the audio already in flight to finish."""
        reply = clean_for_speech(str(call.arguments.get("reply", "")))
        # Drawn now, while the earlier sentences are still playing, so the
        # text and the voice land together rather than one after the other.
        self.ui.speech(reply or stream.spoken)

        async with self._barge_in():
            spoken = await stream.finish()

        self.session.mark_exchange()
        self.brain.remember(command, spoken or reply)

    # -- immediate acknowledgement ----------------------------------------
    def _start_ack(self, call: ToolCall) -> asyncio.Task | None:
        """Tell the user E.V. heard them, without making them wait for it.

        Launching an app or walking a folder tree takes seconds, and silence
        for those seconds reads as "it didn't hear me" - so the user repeats
        themselves and now there are two commands in flight.

        Two things keep this from costing anything:

        * It is a background task. The tool call is already running on its own
          thread by the time the first syllable comes out, so the
          acknowledgement never delays the work it is announcing.
        * It waits `ACK_DELAY_S` first. A tool that finishes in 200ms needs no
          "stand by", and `_finish_ack` cancels the task before it ever
          speaks.

        `chat` is excluded: it has no side effect to wait on, and it is
        already streaming its real reply out sentence by sentence.
        """
        if not config.ACK_ENABLED or call.name == "chat":
            return None

        phrase = _acknowledgement()
        self.ui.note(phrase)  # visual half lands immediately, drawn only
        # `create_task` rather than `ensure_future`: this is only ever called
        # from inside the loop, and it should say so loudly if that changes.
        return asyncio.create_task(self._speak_ack(phrase))

    async def _speak_ack(self, phrase: str) -> None:
        await asyncio.sleep(config.ACK_DELAY_S)
        await self.speaker.say(phrase)

    async def _finish_ack(self, ack: asyncio.Task | None) -> None:
        """Retire the acknowledgement before the real answer is spoken.

        Cancelled outright while it is still waiting, which is the common
        case. Once it is actually speaking it is left to finish: cancelling an
        `asyncio.to_thread` playback does not stop the OS thread, so cutting in
        here would put E.V. on top of itself.
        """
        if ack is None:
            return
        if not self.speaker.speaking:
            ack.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ack

    def _log_backlog(
        self, command: str, call: ToolCall, kind: str, note: str = ""
    ) -> None:
        """Record something E.V. started and did not finish.

        The tool and its arguments ride along so `backlog run` can replay it -
        minus `confirmed`, which `ev.backlog` strips on the way in, so a gated
        command is gated again on the retry.
        """
        if not config.BACKLOG_AUTOLOG or call.name == "chat":
            return
        entry = self.backlog.add(
            command.strip() or _summarise_call(call),
            kind=kind,
            tool=call.name,
            args=dict(call.arguments),
            note=note,
        )
        if entry is not None:
            self.ui.note(f"Backlogged: {entry.describe()}")

    # -- cancelling a running tool ----------------------------------------
    async def _run_tool(
        self, call: ToolCall, arguments: dict | None = None
    ) -> ToolResult:
        """Dispatch one tool, staying interruptible while it runs.

        Two things happen at once here. The tool goes to a worker thread, and
        a listener keeps the microphone open so "stop" can still land - which
        it could not before, because the loop was blocked awaiting the thread.

        Cancellation is cooperative: the token is a request the tool honours
        at a safe point, not a kill. Tools outside `CANCELLABLE` ignore it,
        and `_watch_for_cancel` tells the user so rather than letting them
        believe a launch was called off when it was not.
        """
        token = CancelToken()
        watcher = self._start_cancel_watch(call, token)
        try:
            with self.ui.status("Executing..."):
                return await asyncio.to_thread(
                    dispatch,
                    call.name,
                    call.arguments if arguments is None else arguments,
                    token,
                )
        finally:
            await self._stop_cancel_watch(watcher)

    def _start_cancel_watch(self, call: ToolCall, token: CancelToken):
        """Listen for "stop" for as long as the tool runs, or return None."""
        if not config.CANCEL_ENABLED or self.mic is None or call.name == "chat":
            return None
        stop_watching = threading.Event()
        task = asyncio.create_task(
            self._watch_for_cancel(call, token, stop_watching)
        )
        return task, stop_watching

    async def _stop_cancel_watch(self, watcher) -> None:
        if watcher is None:
            return
        task, stop_watching = watcher
        # Set before cancelling: `mic.listen` polls this every frame, so the
        # worker thread unwinds in milliseconds instead of running on to its
        # own timeout and eating the user's next sentence.
        stop_watching.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _watch_for_cancel(
        self, call: ToolCall, token: CancelToken, stop_watching: threading.Event
    ) -> None:
        """Hear one utterance while a tool runs, and act on it.

        Anything that is not a cancel is *kept*, not discarded. The user
        talking over a slow tool is usually giving the next command, and
        throwing it away would be its own bug.
        """
        cancellable = call.name in CANCELLABLE
        # A tool that returns almost immediately never needs this; opening the
        # microphone for it is pure overhead.
        await asyncio.sleep(config.CANCEL_LISTEN_AFTER_S)

        while not stop_watching.is_set() and self._running:
            utterance = await asyncio.to_thread(
                self.mic.listen,
                2.0,
                lambda: stop_watching.is_set() or not self._running,
            )
            if utterance is None:
                continue
            if stop_watching.is_set():
                return

            heard = await self.transcriber.transcribe(utterance.wav)
            if not heard or getattr(heard, "rejected", False):
                continue

            self.ui.user(str(heard), engaged=True)
            if match_intent(heard, self.session.mode) is not Intent.CANCEL:
                # Held for `_tick`, which uses it instead of listening again.
                self._queued_utterance = heard
                return

            if not cancellable:
                # Honest beats fake. The work is already underway and cannot
                # be unwound, so say that instead of claiming a stop.
                self.ui.warn(f"{call.name} can't be stopped once it's started.")
                self._cancel_refused = call.name
                return

            token.cancel()
            self.speaker.stop()
            self.ui.warn("Stopping...")
            return

    async def _execute(self, command: str, call: ToolCall) -> None:
        # Tools block on subprocesses and the OS, so they run off the loop.
        ack = self._start_ack(call)
        try:
            result: ToolResult = await self._run_tool(call)
        finally:
            await self._finish_ack(ack)

        if result.needs_confirmation:
            # The tool name is held too: file deletes and shell commands both
            # come through here, and resuming the wrong one would be worse
            # than dropping it.
            self.session.pending = {
                "command": command,
                "tool": call.name,
                "args": dict(result.data),
            }
            await self.say(result.speech)
            if not self.text_mode:
                # Confirmation has a short fuse; silence means no.
                reply = await self._next_utterance(wait_s=8.0)
                if reply:
                    self.ui.user(reply, engaged=True)
                await self._resolve_pending(reply)
            return

        await self.say(result.speech)
        # Speech and observation go to different channels. Putting the machine
        # detail in the assistant role is what taught the model to say
        # "Spoke:" out loud - see `ev.brain`.
        self.brain.remember(command, result.speech, result.detail)

        if result.cancelled:
            # The half that did not run is exactly the kind of thing the
            # backlog exists for: stopped on purpose, still unfinished.
            self._log_backlog(command, call, "interrupted", result.detail)
        elif not result.ok:
            # A failure the user heard about is still a thing left undone.
            # Tomorrow it gets read back instead of quietly evaporating.
            self._log_backlog(command, call, "failed", result.detail)

        if self._cancel_refused:
            # Heard "stop" for something that could not honour it. Said out
            # loud rather than left as a silent no-op, because the user is
            # standing there expecting it to have stopped.
            self._cancel_refused = None
            await self.say("That one was already gone. Couldn't call it back.")

    async def _resolve_pending(self, reply: str) -> None:
        pending = self.session.pending
        self.session.pending = None
        if pending is None:
            return

        held = ToolCall(
            pending.get("tool", "terminal_command"), dict(pending.get("args", {}))
        )

        if reply and not is_affirmative(reply) and not is_negative(reply):
            # Not an answer at all - the user moved on. Drop the held command
            # and handle this as a fresh request, instead of silently eating
            # it as a "no" and leaving them wondering where their command went.
            self.brain.remember(
                pending["command"],
                "Never mind.",
                "The user did not answer the confirmation and asked for "
                "something else; the command was not run.",
            )
            # An unanswered confirmation is the textbook interrupted action,
            # so it goes on the list rather than being lost between two
            # sentences.
            self._log_backlog(
                pending["command"],
                held,
                "interrupted",
                "Confirmation went unanswered; never ran.",
            )
            await self.handle(reply)
            return

        if not reply or not is_affirmative(reply):
            await self.say("Cancelled.")
            self.brain.remember(
                pending["command"],
                "Cancelled.",
                "The user declined to confirm; the command was not run.",
            )
            return

        args = {**held.arguments, "confirmed": True}
        args.pop("reason", None)
        ack = self._start_ack(held)
        try:
            result: ToolResult = await self._run_tool(held, args)
        finally:
            await self._finish_ack(ack)
        await self.say(result.speech)
        self.brain.remember(pending["command"], result.speech, result.detail)
        if not result.ok:
            self._log_backlog(pending["command"], held, "failed", result.detail)

    # -- one-shot ---------------------------------------------------------
    async def run_once(self, command: str) -> None:
        self._running = True
        # One-shot mode skips `start`, but it still wants the user's stored
        # preferences and the open backlog in context.
        self._boot()
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
    import os
    import shutil

    from tools.app_launcher import app_index as get_app_index

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

    notes.append(
        f"OK   streaming: {'on' if config.LLM_STREAMING else 'off'} "
        "(speech starts on the first finished sentence)"
    )

    if config.STT_PROVIDER == "groq" and not config.GROQ_API_KEY:
        problems.append("MISS stt: groq backend needs GROQ_API_KEY")
    else:
        notes.append(f"OK   stt: {config.STT_PROVIDER} ({config.GROQ_STT_MODEL})")
        if config.STT_PROVIDER == "groq":
            notes.append(
                f"OK   stt gate: {'on' if config.STT_CONFIDENCE_GATE else 'off'} "
                f"(re-ask below {config.STT_MIN_LOGPROB:.2f} logprob, "
                f"flag below {config.STT_UNCERTAIN_LOGPROB:.2f})"
            )
        else:
            notes.append(
                f"WARN stt gate: {config.STT_PROVIDER} reports no confidence; "
                "the gate has nothing to act on"
            )

    # A shortcut index is what lets E.V. launch programs that were never added
    # to PATH and were never written into APP_ALIASES.
    if config.APP_INDEX_ENABLED:
        indexed = len(get_app_index().apps())
        if indexed:
            notes.append(f"OK   apps: {indexed} Start Menu shortcuts indexed")
        else:
            notes.append(
                "WARN apps: no Start Menu shortcuts indexed; only APP_ALIASES "
                "and PATH will resolve"
            )
    else:
        notes.append("WARN apps: Start Menu index disabled (EV_APP_INDEX_ENABLED=false)")

    for module, label, required in (
        ("edge_tts", "edge-tts (voice output)", config.TTS_ENABLED),
        ("sounddevice", "sounddevice (microphone)", True),
        ("httpx", "httpx (API calls)", True),
        ("rich", "rich (terminal UI)", False),
        ("pyautogui", "pyautogui (dev_tools keystrokes)", False),
        ("send2trash", "send2trash (recoverable deletes)", False),
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

    # File roots decide what `file_manager` is allowed to touch, so they are
    # worth stating plainly rather than leaving in a config file.
    for root in config.FILE_ROOTS:
        state = "OK  " if root.is_dir() else "WARN"
        notes.append(f"{state} files: {root} {'' if root.is_dir() else '(missing)'}")
    notes.append(f"OK   files: new files default to {config.FILE_DEFAULT_DIR}")

    # Persisted state is the thing that makes a restart feel continuous, so a
    # directory E.V. cannot write to is worth saying out loud rather than
    # discovering as a silently forgetful assistant.
    if config.MEMORY_ENABLED or config.BACKLOG_ENABLED:
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            writable = os.access(config.STATE_DIR, os.W_OK)
        except OSError as exc:
            writable = False
            notes.append(f"WARN state: could not create {config.STATE_DIR} ({exc})")
        if writable:
            notes.append(f"OK   state: {config.STATE_DIR}")
        else:
            problems.append(
                f"MISS state: {config.STATE_DIR} is not writable - memory and "
                "backlog will not survive a restart"
            )

    if config.MEMORY_ENABLED:
        memory = get_memory()
        facts = len(memory.preferences) + len(memory.profile)
        notes.append(
            f"OK   memory: {config.MEMORY_FILE.name} ({facts} stored fact(s))"
        )
    else:
        notes.append("WARN memory: disabled (EV_MEMORY_ENABLED=false)")

    if config.BACKLOG_ENABLED:
        outstanding = len(get_backlog().pending())
        notes.append(
            f"OK   backlog: {config.BACKLOG_FILE.name} ({outstanding} item(s) open)"
        )
    else:
        notes.append("WARN backlog: disabled (EV_BACKLOG_ENABLED=false)")

    notes.append(
        f"OK   acknowledgement: {'on' if config.ACK_ENABLED else 'off'} "
        f"(spoken after {config.ACK_DELAY_S:.2f}s on a slow tool)"
    )

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
