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
import time

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
    split_followup,
)
from ev.stt import Transcriber, TranscriptionError, Transcript
from ev.tts import Speaker, SpeechStream, clean_for_speech
from ev.ui import UI
from tools import CANCELLABLE, CancelToken, ToolResult, dispatch, from_model
from tools.computer_use import close_vision_client
from tools.web_agent import close_planner_client
from tools.guard import (
    audit,
    engage_lockdown,
    is_locked_down,
    lockdown_reason,
    release_lockdown,
)
from tools.safety import is_affirmative, is_high_risk, is_negative

log = logging.getLogger("ev")


def _describe(call: ToolCall) -> str:
    """One-line summary of a tool call, for the terminal only."""
    interesting = (
        "app", "query", "url", "action", "path", "command", "directory",
        # Computer use: the label and the goal are the whole story of
        # what a click is about to do, so they belong on screen.
        "label", "task", "question", "keys",
    )
    parts = [
        f"{key}={value}" 
        for key, value in call.arguments.items()
        if key in interesting and str(value).strip()
    ]
    return "  ".join(parts)[:100]


# Handed to the model when Whisper flagged the transcript as unclear. It is
# advice, not an instruction to stop: most fuzzy transcripts are perfectly
# actionable, and refusing every one of them would be worse than the guessing.
# Handed to the model on the second turn of a compound request. It has to be
# explicit that the first half already ran: without it the model re-reads the
# whole original request and opens the thing a second time.
_FOLLOW_UP = (
    "This is the second half of the request the user just made. The first "
    "half has already been carried out - the result of it is in the "
    "conversation above. Answer only what is left. If what just happened put "
    "something on screen, take_screenshot is how you read it. If the request "
    "has in fact already been handled in full, say so in one short line with "
    "chat rather than doing anything again."
)


_UNCLEAR_AUDIO = (
    "The speech recognition was unsure about that transcript, so some words "
    "may be wrong. If the request is clear enough to act on, act on it. If a "
    "misheard word would make you do the wrong thing, use chat to ask the "
    "user to repeat it instead of guessing."
)


# Tools that take over the screen. They earn a different acknowledgement
# from the generic "stand by": while one of these runs, the pointer moves on
# its own and windows change under the user's hands. Being told that is about
# to happen, before the first click rather than after it, is the difference
# between "it's working" and "something has taken over my mouse".
SCREEN_TOOLS: frozenset[str] = frozenset(
    {"screen_task", "browser_task", "mouse_action", "keyboard_action", "agent_task"}
)


def _acknowledgement(tool: str = "") -> str:
    """A short "still here, working on it" line. Plain speech, no labels."""
    if tool in SCREEN_TOOLS and config.ACK_SCREEN_PHRASE:
        return config.ACK_SCREEN_PHRASE
    return random.choice(config.ACK_PHRASES) if config.ACK_PHRASES else "On it."


class TurnClock:
    """Where one turn's seconds went, for the log line.

    Deliberately a plain stopwatch rather than anything cleverer. The point
    is to be able to say "the model took 0.9s and the voice took 0.8s"
    instead of "it feels slow", because those two have different fixes and
    no amount of reasoning about the code tells you which one you have.
    """

    __slots__ = ("started", "stages", "_mark")

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self._mark = self.started
        self.stages: list[tuple[str, float]] = []

    def lap(self, name: str) -> None:
        """Record the time since the last lap under `name`."""
        now = time.perf_counter()
        self.stages.append((name, now - self._mark))
        self._mark = now

    def report(self) -> str:
        parts = " ".join(f"{name} {secs:.2f}s" for name, secs in self.stages)
        return f"{parts} | total {time.perf_counter() - self.started:.2f}s"


def _summarise_call(call: ToolCall) -> str:
    """How a backlog entry should read when it is handed back tomorrow."""
    described = _describe(call)
    return f"{call.name}: {described}" if described else call.name


class EV:
    """Owns the session: audio devices, network clients, and history."""

    # A class-level default, not only an `__init__` one. Several tests build
    # an `EV` with `__new__` and set up just the handful of attributes they
    # need, which is the right way to test a loop that owns a microphone and
    # two network clients - and it means anything read on a code path they
    # exercise has to have a value without `__init__` having run.
    _clock: "TurnClock | None" = None

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
        # The trailing question of a compound request, held while the first
        # half runs. Survives a confirmation, so "open the drive and tell me
        # what's in it" still answers after a spoken yes.
        self._pending_followup: str = ""
        # Stopwatch for the turn in progress, or None outside one.
        self._clock: TurnClock | None = None

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

    def _user_name(self) -> str:
        """What to call the user out loud.

        A stored fact outranks the configured default, so "remember my name is
        Al" changes the greeting from the next start onwards without anyone
        touching `.env`. Both are optional: with neither, the greeting simply
        drops the name rather than addressing the user as an empty string.
        """
        for key in ("name", "user name", "my name"):
            stored = self.memory.recall(key)
            if stored:
                return stored.strip()
        return config.USER_NAME.strip()

    async def _report_state(self) -> None:
        """Say what carried over from last time.

        Spoken, because the whole point of a backlog is being told about it
        rather than having to go and look.

        The greeting itself is not conditional on there being news. A voice
        assistant that starts up silent is indistinguishable from one that
        failed to start, and the user is looking at a terminal rather than at
        a status light.
        """
        report = self._boot_report
        if report is None:
            return

        greeting = (
            report.greeting_for(self._user_name())
            if config.GREET_ON_START
            else report.greeting
        )
        if greeting:
            await self.say(greeting)

        # Before the backlog, because "here is what is outstanding" makes no
        # sense from something that is not allowed to do any of it.
        if is_locked_down():
            await self.say(
                "Heads up, I'm locked down - I can talk and look but not act. "
                "Say 'unlock' to lift it."
            )
            self.ui.warn(f"Lockdown active: {lockdown_reason()}")

        summary = self.backlog.summary()
        if summary:
            await self.say(summary)
            self.ui.note(self.backlog.listing())

        # The user's own list, after E.V.'s. They are different things and
        # saying so in two sentences is what keeps them from sounding like
        # one merged pile of unfinished business.
        todos = self.memory.todo_summary()
        if todos:
            await self.say(todos)

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
                # Clear the level history as each reply starts playing. The
                # tail of the user's own command is still counted as recent
                # speech otherwise, and E.V. barges in on its own first word.
                self.speaker.on_playback_start = self.mic.reset_levels
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
                    all_responses()
                    + ["Yeah?"]
                    + list(config.ACK_PHRASES)
                    + ([config.ACK_SCREEN_PHRASE] if config.ACK_SCREEN_PHRASE else [])
                )
            )

    async def stop(self) -> None:
        self._running = False
        self.ui.end_status()
        self.speaker.stop()
        if self.mic is not None:
            await asyncio.to_thread(self.mic.close)
            self.mic = None
        # Marks the shutdown clean, which is what keeps the next start from
        # reporting a crash that did not happen.
        self.memory.end_session(clean=True)
        await self._http.aclose()
        # The vision client is opened lazily by the first screenshot and may
        # never exist at all; closing it is a no-op when it does not.
        close_vision_client()
        close_planner_client()

    def request_stop(self) -> None:
        self._running = False
        self.ui.end_status()
        self.speaker.stop()

    # -- input ------------------------------------------------------------
    async def _next_utterance(self, wait_s: float | None) -> str:
        """One transcript from the microphone, or an empty string."""
        if self.mic is None:
            return ""

        # The spinner is shown only while the conversation is open, and that
        # is not a detail of presentation. Animated the whole time, it says
        # E.V. is listening to the room - which it is, but not to *you*:
        # outside the window nothing is acted on without the wake phrase, and
        # a spinner that spins through that is a machine claiming attention
        # it is not paying. Inside the window it is the honest signal that
        # the next thing said needs no name in front of it.
        listening = self.session.engaged and not self.session.in_standby
        if listening:
            self.ui.begin_status("Listening...")
        else:
            self.ui.end_status()

        utterance = await asyncio.to_thread(
            self.mic.listen, wait_s, lambda: not self._running
        )
        if utterance is None:
            return ""
        # Whatever happens next prints, and the spinner has to be gone first.
        self.ui.end_status()

        # In standby the only thing worth hearing is a couple of words. A
        # cough or a passing sentence is not, and transcribing it costs a Groq
        # call for something that is going to be dropped anyway.
        if self.session.in_standby and utterance.duration_s > config.STANDBY_MAX_UTTERANCE_S:
            log.debug("Ignoring %.1fs of room noise while in standby", utterance.duration_s)
            return ""

        try:
            self._clock = TurnClock()
            # Shown on the same terms as the listening one. An unaddressed
            # sentence is still transcribed - detecting the wake phrase means
            # having the words - but announcing that on screen is the same
            # claim of attention, made about a conversation that was not with
            # E.V. at all.
            watching = (
                self.ui.status("Transcribing...")
                if listening
                else contextlib.nullcontext()
            )
            with watching:
                transcript = await self.transcriber.transcribe(utterance.wav)
            self._clock.lap("stt")
            # Deliberately *not* fed to the decoding prompt here. That
            # happens in `_route`, once E.V. knows the words were meant for
            # it: a conversation happening across the room would otherwise
            # steer the recogniser for the command that follows it.
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
            # Nothing left after cleaning. Silence here is what makes E.V.
            # look dead rather than busy - the user said something, watched an
            # empty terminal, and repeated themselves. Drawn, never spoken:
            # inventing words to say would breach the speech boundary, but
            # saying nothing *and* showing nothing is a bug, not a policy.
            if text and text.strip():
                log.warning("Reply was nothing but formatting: %.120s", text)
                self.ui.warn("I had nothing to say to that.")
            return
        self.ui.speech(spoken)

        # Lapped before playback rather than after: what matters is how long
        # the user waited for the first sound, not how long the sentence
        # takes to read out.
        clock = self._clock
        self._clock = None
        async with self._barge_in():
            speaking = asyncio.get_running_loop().time()
            await self.speaker.say(spoken)
        if clock is not None:
            clock.stages.append(
                ("voice", asyncio.get_running_loop().time() - speaking)
            )
            self._log_timing(clock)

        # Replying keeps the conversation open, so the user's next sentence
        # needs no wake phrase.
        self.session.mark_exchange()
        # Throttled internally, so this is a no-op on most turns. It bounds
        # how much of a session a power cut can erase.
        self.memory.touch()

    def _log_timing(self, clock: TurnClock) -> None:
        """One line per turn, on the terminal when asked and always in the log."""
        report = clock.report()
        log.info("turn: %s", report)
        if config.TURN_TIMING:
            self.ui.note(f"timing: {report}")

    async def _watch_for_barge_in(self) -> None:
        """Cut playback the moment the user starts talking over E.V.

        Two independent conditions, because either one alone has a failure
        mode that makes E.V. unusable in the opposite direction:

        * **Sustained.** A door closing is loud and over in one frame.
          `BARGE_IN_FRAMES` is what stops a cough taking the floor.
        * **Louder than E.V.** On loudspeakers E.V. hears its own voice, which
          is a long, steady run of speech-looking frames - exactly what the
          frame count is looking for. Left at that, E.V. interrupts itself on
          every single reply. The level test is what separates the user from
          the loopback.

        On a trigger the queued audio is *kept* rather than flushed. Those
        frames are the opening of the user's sentence, and throwing them away
        makes them say it twice - which is the thing barge-in exists to stop.
        """
        if self.mic is None:
            return
        # E.V.'s own attack is the loudest thing this microphone will hear all
        # sentence. Cutting itself off on its own first syllable is the one
        # barge-in failure with no recovery, so the grace period comes first.
        await asyncio.sleep(config.BARGE_IN_GRACE_S)
        threshold = self.mic.noise_floor * config.BARGE_IN_LEVEL_MULTIPLIER
        while self.speaker.speaking:
            if (
                self.mic.speech_energy() >= config.BARGE_IN_FRAMES
                and self.mic.speech_level() >= threshold
            ):
                log.info(
                    "Barge-in: %d frames at %.4f (floor %.4f)",
                    self.mic.speech_energy(),
                    self.mic.speech_level(),
                    self.mic.noise_floor,
                )
                if config.BARGE_IN_KEEP_AUDIO:
                    self.mic.hold_audio()
                self.speaker.stop()
                self.ui.note("Go ahead.")  # drawn, never spoken
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

        if self.text_mode and transcript.lower() in {"quit", "exit"}:
            self.request_stop()
            return

        # The wake check comes *before* the echo, and that order is the whole
        # point. Drawn first, every sentence spoken near the microphone
        # appeared on screen under a "you >" prompt whether or not it was
        # addressed to E.V. - so a conversation with somebody else in the room
        # was transcribed into E.V.'s terminal, looked like it had been heard
        # and understood, and then got no reply. Silence is the correct answer
        # to something that was not said to you, and it has to look like
        # silence too.
        engaged = self.session.engaged
        command = self._extract_command(transcript)
        if command is None:
            return

        if not self.text_mode:
            # `engaged` is read before `_extract_command`, which engages the
            # session on a wake phrase - otherwise the marker would claim the
            # conversation was already open when this utterance is what opened
            # it.
            self.ui.user(transcript, engaged=engaged)

        await self._route(transcript, command)

    async def _route(self, transcript: "Transcript | str", command: str) -> None:
        """Decide what one addressed utterance means, and act on it.

        Split out of `_tick` so an utterance heard *during* a running tool can
        take exactly the same path afterwards. Anything held that way has
        already been transcribed and echoed; re-listening for it would make
        the user say it twice.
        """
        # Names and jargon carry across a conversation, and Whisper reads the
        # decoding prompt as text immediately preceding the audio. Fed here
        # rather than at transcription time, so only words that were actually
        # meant for E.V. shape what it expects to hear next.
        if transcript and not getattr(transcript, "rejected", False):
            self.transcriber.note_transcript(transcript)

        # Checked only once E.V. knows it was being spoken to. A rejected
        # transcript from across the room should stay silent, exactly like any
        # other utterance that was not addressed here.
        if getattr(transcript, "rejected", False):
            # `why` is a `Transcript` method, and `transcript` is only one of
            # those when it came from the recogniser - a typed command or a
            # held utterance arrives as a plain `str`.
            why = transcript.why() if isinstance(transcript, Transcript) else "unclear"
            self.ui.warn(f"Didn't catch that clearly ({why}).")
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

        if intent is Intent.LOCKDOWN:
            # Matched locally, like every other control phrase, and for the
            # sharpest version of the usual reason: this is the phrase
            # someone says while watching E.V. do something they did not ask
            # for. Sending it to the model first would mean asking the thing
            # that is misbehaving for permission to stop it.
            self.session.pending = None
            self._pending_followup = ""
            engage_lockdown("the user said so")
            await self.say(response_for(intent))
            self.ui.note(
                "Lockdown: tools that change anything are refused. "
                "Say 'unlock' to lift it."
            )
            return

        if intent is Intent.UNLOCK:
            released = release_lockdown()
            await self.say(
                response_for(intent) if released else "Nothing was locked."
            )
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

    async def handle(
        self,
        command: str,
        uncertain: bool = False,
        extra_context: str = "",
        allow_chain: bool = True,
    ) -> None:
        """Route one command: confirmation reply, or a fresh model turn.

        `uncertain` means Whisper was not confident in the transcript. It is
        passed to the model as context rather than acted on here: E.V. cannot
        tell whether a fuzzy transcript is ambiguous, but the model reading it
        alongside the request can, and can ask instead of guessing.

        `allow_chain` is false on the second turn of a compound request, so a
        follow-up cannot spawn a follow-up of its own.
        """
        if self.session.pending is not None:
            await self._resolve_pending(command)
            return

        # "Open my mail and give me a summary" is two jobs. Held now, while
        # the words are still here to split - after the model has answered,
        # all that is left is one tool call and no sign there was more.
        followup = (
            split_followup(command)
            if allow_chain and config.CHAIN_ENABLED
            else ""
        )

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
                    extra_context=self._context_for(uncertain, extra_context),
                    on_sentence=on_sentence if self._streaming else None,
                )
        except BrainError as exc:
            if stream is not None:
                stream.cancel()
            log.warning("Brain error: %s", exc)
            await self.say(str(exc))
            return

        if self._clock is not None:
            self._clock.lap("brain")
        log.info("tool=%s args=%s", call.name, call.arguments)
        self.ui.action(call.name, _describe(call))

        if stream is not None:
            if call.name == "chat":
                await self._finish_streamed(command, call, stream)
                return
            # Only `chat` streams, so this should not happen - but an
            # abandoned stream would hold the speaker lock forever.
            stream.cancel()

        await self._execute(command, call, followup=followup)

    @staticmethod
    def _context_for(uncertain: bool, extra: str) -> str:
        """Join the per-turn notes the model should read before deciding."""
        pieces = [extra, _UNCLEAR_AUDIO if uncertain else ""]
        return " ".join(piece for piece in pieces if piece)

    async def _finish_streamed(
        self, command: str, call: ToolCall, stream: SpeechStream
    ) -> None:
        """Show the reply and wait for the audio already in flight to finish."""
        reply = clean_for_speech(str(call.arguments.get("reply", "")))
        # Drawn now, while the earlier sentences are still playing, so the
        # text and the voice land together rather than one after the other.
        # Both halves can be empty if the model produced nothing usable, and
        # an empty panel reads as a crash.
        self.ui.speech(reply or stream.spoken or "(nothing came back)")

        clock = self._clock
        self._clock = None
        async with self._barge_in():
            spoken = await stream.finish()
        if clock is not None:
            # No "voice" lap here: a streamed reply started talking while the
            # model was still writing it, which is the whole point of the
            # streaming path and is already inside the brain lap.
            self._log_timing(clock)

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

        phrase = _acknowledgement(call.name)
        self.ui.note(phrase)  # visual half lands immediately, drawn only
        # A screen task is announced with no delay at all. The usual argument
        # for waiting - that a fast tool needs no "stand by" - does not apply
        # to something that is about to move the pointer.
        delay = (
            config.ACK_SCREEN_DELAY_S
            if call.name in SCREEN_TOOLS
            else config.ACK_DELAY_S
        )
        # `create_task` rather than `ensure_future`: this is only ever called
        # from inside the loop, and it should say so loudly if that changes.
        return asyncio.create_task(self._speak_ack(phrase, delay))

    async def _speak_ack(self, phrase: str, delay: float | None = None) -> None:
        await asyncio.sleep(config.ACK_DELAY_S if delay is None else delay)
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
                # `arguments` is only ever passed by the replay of a spoken
                # yes. Anything else is the model's own call, which may not
                # confirm itself - see `tools.CONFIRMATION_ONLY_ARGS`.
                return await asyncio.to_thread(
                    dispatch,
                    call.name,
                    from_model(call.arguments) if arguments is None else arguments,
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
        if self.mic is None:
            # `_start_cancel_watch` already checked, but this runs as its own
            # task: by the time it does, `stop()` may have closed the device
            # and set this to None. Narrowing it here is also what tells the
            # type checker that `self.mic.listen` below is real.
            return

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
            intent = match_intent(heard, self.session.mode)

            if intent is Intent.LOCKDOWN:
                # The one phrase that must work best while a tool is
                # running, because that is when it is said. "Stop" is about
                # the thing in front of the user; "stop everything" is about
                # everything after it too, and holding it until the running
                # tool finished would answer the wrong question - an
                # autonomous run would keep going for another minute while
                # E.V. sat on the sentence asking it not to.
                engage_lockdown("the user said so")
                self.session.pending = None
                self._pending_followup = ""
                token.cancel()
                self.speaker.stop()
                self.ui.warn("Lockdown: everything stops until you say unlock.")
                await self.say(response_for(Intent.LOCKDOWN))
                return

            if intent is not Intent.CANCEL:
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

    async def _execute(
        self, command: str, call: ToolCall, followup: str = ""
    ) -> None:
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
            # Held across the question, so "open the drive and tell me what's
            # in it" still answers the second half after a spoken yes.
            self._pending_followup = followup
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
        #
        # `untrusted` rides along so a page, a file or a screen is fenced on
        # the way back into the request rather than arriving looking like
        # something the user said.
        self.brain.remember(
            command, result.speech, result.detail, untrusted=result.untrusted
        )

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
            return

        if followup and result.ok and not result.cancelled:
            await self._run_followup(followup)

    async def _run_followup(self, question: str) -> None:
        """Answer the trailing question of a compound request.

        Depth one, always. A follow-up that could spawn its own follow-up is
        a loop with no ceiling, and the thing being fixed here is a request
        losing half of itself - not E.V. needing to plan.

        The settle wait is not politeness: the first tool has usually just
        launched something, and a screenshot taken before the window has
        drawn describes whatever was there before it.
        """
        self.ui.note(f"Still to do: {question}")
        if config.CHAIN_SETTLE_S > 0:
            await asyncio.sleep(config.CHAIN_SETTLE_S)
        await self.handle(question, extra_context=_FOLLOW_UP, allow_chain=False)

    async def _resolve_pending(self, reply: str) -> None:
        pending = self.session.pending
        self.session.pending = None
        # Claimed here rather than read later: every path out of this method
        # ends the exchange, and a leftover question would then surface on
        # whatever the user said next.
        followup, self._pending_followup = self._pending_followup, ""
        if pending is None:
            return

        held = ToolCall(
            pending.get("tool", "terminal_command"), dict(pending.get("args", {}))
        )
        # How firm a yes this one needs. "Go" is a fine way to confirm
        # opening an app and a poor way to confirm a delete: it is one
        # syllable, the microphone is open, and the room is full of them.
        strict = config.STRICT_CONFIRM_HIGH_RISK and is_high_risk(
            str(pending.get("args", {}).get("reason", ""))
        )
        yes = is_affirmative(reply, strict=strict)

        # A confirmation answered by a transcript the recogniser itself was
        # unsure of is not a confirmation. Re-asking costs one sentence;
        # guessing costs whatever the command does.
        if (
            yes
            and config.CONFIRM_REQUIRES_CLEAR_AUDIO
            and getattr(reply, "uncertain", False)
        ):
            self.session.pending = pending
            self._pending_followup = followup
            self.ui.warn("That yes was unclear; asking again.")
            await self.say("I didn't hear that clearly. Confirm?")
            if not self.text_mode:
                second = await self._next_utterance(wait_s=8.0)
                if second:
                    self.ui.user(second, engaged=True)
                await self._resolve_pending(second)
            return

        if reply and not yes and not is_negative(reply):
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

        if not reply or not yes:
            await self.say("Cancelled.")
            self.brain.remember(
                pending["command"],
                "Cancelled.",
                "The user declined to confirm; the command was not run.",
            )
            return

        args = {**held.arguments, "confirmed": True}
        args.pop("reason", None)
        audit("confirmed", tool=held.name, command=str(pending["command"])[:200])
        ack = self._start_ack(held)
        try:
            result: ToolResult = await self._run_tool(held, args)
        finally:
            await self._finish_ack(ack)
        await self.say(result.speech)
        self.brain.remember(
            pending["command"],
            result.speech,
            result.detail,
            untrusted=result.untrusted,
        )
        if not result.ok:
            self._log_backlog(pending["command"], held, "failed", result.detail)
            return

        if followup and not result.cancelled:
            await self._run_followup(followup)

    # -- one-shot ---------------------------------------------------------
    async def run_once(self, command: str) -> None:
        self._running = True
        # One-shot mode skips `start`, but it still wants the user's stored
        # preferences and the open backlog in context.
        self._boot()
        # And it still needs a model that exists. Skipping this is what made
        # `--say` answer a perfectly ordinary command with a 404 from a model
        # name the interactive path would have quietly fallen back from.
        try:
            await self.brain.verify_model()
        except BrainError as exc:
            self.ui.error(str(exc))
            self._running = False
            return
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
    from pathlib import Path

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

                    # How many separate token budgets this key actually has.
                    # Groq meters tokens per minute per model, so this is the
                    # number that decides how many commands fit in a minute -
                    # and it is the first thing worth knowing when someone is
                    # hitting rate limits. One bucket is worth saying out
                    # loud, because it is a fixable state rather than a fact.
                    probe = Brain.__new__(Brain)
                    probe._build_rotation(available)
                    rotation = probe._groq_rotation
                    per_minute = 8000 * len(rotation)
                    if len(rotation) > 1:
                        notes.append(
                            f"OK   token buckets: {len(rotation)} "
                            f"({', '.join(rotation)}) - Groq meters per model, "
                            f"so that is ~{per_minute} tokens a minute, not 8000"
                        )
                    else:
                        notes.append(
                            "WARN token buckets: 1. Groq meters tokens per minute "
                            "per model, so a second usable model on this key would "
                            "double the budget. Add one to EV_GROQ_MODEL_ROTATION."
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
        ("pyautogui", "pyautogui (keystrokes and mouse control)", False),
        ("send2trash", "send2trash (recoverable deletes)", False),
        ("mss", "mss (fast screen capture)", False),
        ("PIL", "Pillow (screenshot downscaling)", False),
        ("playwright", "playwright (browser_task)", False),
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

    # The guardrails, stated plainly. Every one of them is invisible when it
    # is working, which is exactly why a readiness report is the only place
    # the user ever finds out whether they are on.
    from tools.guard import audit_path, is_locked_down

    if config.LOCKDOWN_ENABLED:
        state = "ENGAGED - tools that change anything will refuse" if is_locked_down() else "ready"
        notes.append(f"OK   lockdown: {state} (say 'lockdown' / 'unlock')")
    else:
        problems.append(
            "MISS lockdown: EV_LOCKDOWN_ENABLED is false - there is no spoken "
            "way to take E.V.'s tools away mid-session"
        )

    if config.GUARD_ENABLED:
        notes.append(
            f"OK   runaway limiter: {config.GUARD_MAX_ACTIONS} actions per "
            f"{int(config.GUARD_WINDOW_S)}s, {config.GUARD_MAX_REPEATS} identical in a row"
        )
    else:
        notes.append("WARN runaway limiter: disabled (EV_GUARD_ENABLED=false)")

    if config.AUDIT_ENABLED:
        path = audit_path()
        size = path.stat().st_size if path.exists() else 0
        notes.append(f"OK   audit log: {path} ({size} bytes)")
    else:
        notes.append(
            "WARN audit log: disabled (EV_AUDIT_ENABLED=false) - nothing "
            "records what E.V. did while you were not watching"
        )

    notes.append(
        f"OK   redaction: {'on' if config.REDACT_SECRETS else 'off'}  "
        f"untrusted fencing: {'on' if config.UNTRUSTED_FENCING else 'off'}"
    )
    if not config.REDACT_SECRETS:
        problems.append(
            "MISS redaction: EV_REDACT_SECRETS is false - a file read that "
            "turns out to hold a key will send it to the model"
        )
    if not config.UNTRUSTED_FENCING:
        problems.append(
            "MISS untrusted fencing: EV_UNTRUSTED_FENCING is false - page and "
            "file text reaches the model looking like the user said it"
        )

    # FILE_ROOTS defaulting to the whole profile is a deliberate choice and a
    # broad one. Worth saying out loud once rather than leaving to be found.
    if any(root == Path.home() for root in config.FILE_ROOTS):
        notes.append(
            "WARN files: FILE_ROOTS is your whole home folder. Credential "
            "files are refused by name, but a narrower EV_FILE_ROOTS is safer"
        )

    # Screen control has no sandbox and no undo, so the readiness report says
    # out loud what is armed rather than leaving it buried in a config file.
    if config.COMPUTER_USE_ENABLED:
        from tools.computer_use import screen_size

        width, height = screen_size()
        if width and height:
            notes.append(f"OK   screen: {width}x{height}")
        else:
            notes.append("WARN screen: could not determine the desktop size")
        if importlib.util.find_spec("mss") or importlib.util.find_spec("PIL"):
            notes.append("OK   screen capture: available")
        else:
            problems.append(
                "MISS screen capture: install mss (and Pillow) or take_screenshot "
                "and screen_task cannot see anything"
            )
        if config.COMPUTER_CONFIRM_RISKY:
            notes.append(
                "OK   computer use: on, risky actions held for a spoken yes"
            )
        else:
            problems.append(
                "MISS computer use: EV_COMPUTER_CONFIRM_RISKY is false - "
                "purchases, sends and deletes will run unasked"
            )
    else:
        notes.append("WARN computer use: disabled (EV_COMPUTER_USE_ENABLED=false)")

    if config.VISION_ENABLED:
        provider = config.VISION_PROVIDER
        vision_key = config.GROQ_API_KEY if provider == "groq" else config.GEMINI_API_KEY
        if not vision_key:
            problems.append(f"MISS vision: {provider} needs an API key")
        elif provider != "groq":
            notes.append(f"OK   vision: gemini ({config.GEMINI_VISION_MODEL})")
        else:
            # Groq's vision catalogue turns over faster than its chat one and
            # differs per account, so naming the rung this machine will
            # actually land on beats printing the configured default and
            # letting the user find out at the first screenshot.
            ladder = [config.GROQ_VISION_MODEL, *config.GROQ_VISION_FALLBACKS]
            try:
                import httpx as _httpx

                catalogue = _httpx.get(
                    f"{config.GROQ_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {vision_key}"},
                    timeout=10.0,
                )
                names = (
                    {item["id"] for item in catalogue.json().get("data", [])}
                    if catalogue.status_code == 200
                    else None
                )
            except Exception as exc:
                names = None
                notes.append(f"WARN vision: could not verify ({exc})")

            if names is None:
                notes.append(f"OK   vision: groq ({config.GROQ_VISION_MODEL}, unverified)")
            else:
                landing = next((name for name in ladder if name in names), None)
                if landing == config.GROQ_VISION_MODEL:
                    notes.append(f"OK   vision: groq ({landing})")
                elif landing:
                    notes.append(
                        f"WARN vision: '{config.GROQ_VISION_MODEL}' unavailable; "
                        f"will fall back to '{landing}'"
                    )
                else:
                    problems.append(
                        "MISS vision: none of "
                        f"{', '.join(ladder)} exist on this account. Set "
                        "EV_GROQ_VISION_MODEL to a vision model you do have."
                    )
    else:
        notes.append("WARN vision: disabled (EV_VISION_ENABLED=false)")

    if config.BROWSER_AUTOMATION_ENABLED:
        if importlib.util.find_spec("playwright") is None:
            notes.append(
                "WARN browser_task: playwright not installed - "
                "pip install playwright && python -m playwright install chromium"
            )
        else:
            notes.append(
                f"OK   browser_task: {config.BROWSER_ENGINE}, "
                f"{'headless' if config.BROWSER_HEADLESS else 'headed'}"
            )
    else:
        notes.append(
            "WARN browser_task: disabled (EV_BROWSER_AUTOMATION_ENABLED=false)"
        )

    if config.AGENT_MODE_ENABLED:
        from tools.overlay import parse_hotkey

        notes.append(
            f"OK   autonomous missions: on, up to {config.AGENT_MAX_ROUNDS} "
            f"rounds or {config.AGENT_TIMEOUT_S:.0f}s; overlay "
            f"{'on' if config.AGENT_OVERLAY_ENABLED else 'OFF'}"
        )
        # Which route a mission takes first is the difference between an
        # errand that runs for twenty rounds and one that is rate limited
        # after four, so it belongs in the readiness report rather than in
        # the logs.
        if config.AGENT_PREFER_BROWSER and config.BROWSER_AUTOMATION_ENABLED:
            from tools.web_agent import planner_rotation

            if importlib.util.find_spec("playwright") is None:
                notes.append(
                    "WARN missions: browser-first is on but playwright is not "
                    "installed, so every errand will fall back to vision and "
                    "meet the per-minute limit in about four rounds"
                )
            else:
                notes.append(
                    f"OK   mission route: browser first, up to "
                    f"{config.AGENT_WEB_MAX_ROUNDS} rounds with no vision cost"
                )
                notes.append(
                    f"OK   planner: {' -> '.join(planner_rotation())}"
                )
                # Both of these are quiet when they are wrong: an errand with
                # the guard off simply does the irreversible thing twice, and
                # one with no looks left decides from text it cannot read.
                looks = (
                    f"up to {config.AGENT_WEB_LOOK_MAX} screenshot(s)"
                    if config.AGENT_WEB_LOOK_ENABLED
                    else "no screenshots"
                )
                guard = "on" if config.AGENT_WEB_REPEAT_GUARD else "OFF"
                notes.append(f"OK   browser errands: repeat guard {guard}, {looks}")
        else:
            notes.append(
                "WARN mission route: vision only - each round costs ~1900 "
                "tokens, so expect about four before the per-minute limit"
            )
        # The kill switch is the one setting where a typo is silent and
        # expensive: the run still happens, and the way out of it does not.
        if not config.AGENT_HOTKEY_ENABLED:
            notes.append(
                "WARN kill switch: hotkey disabled - stopping a mission means "
                "saying 'stop everything' or Ctrl+C"
            )
        elif parse_hotkey(config.AGENT_KILL_HOTKEY) is None:
            problems.append(
                f"MISS kill switch: EV_AGENT_KILL_HOTKEY='{config.AGENT_KILL_HOTKEY}' "
                "is not a modifier plus a key, so no hotkey will be registered"
            )
        else:
            notes.append(f"OK   kill switch: {config.AGENT_KILL_HOTKEY}")
    else:
        notes.append("WARN autonomous missions: disabled (EV_AGENT_MODE_ENABLED=false)")

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
