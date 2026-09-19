"""Centralised configuration for E.V. (Everyday Virtual Assistant).

Every value can be overridden from the environment (see `.env.example`).
Importing this module only allocates a handful of strings, so it is
effectively free in RAM terms.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """Minimal .env loader.

    Avoids a hard dependency on python-dotenv; if the real package happens to
    be installed we defer to it because it handles quoting edge cases better.
    """
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    value = _env(key).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_list(key: str, default: list[str]) -> list[str]:
    value = _env(key)
    if not value:
        return default
    return [item.strip().lower() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Brain (LLM)
# ---------------------------------------------------------------------------
LLM_PROVIDER = _env("EV_LLM_PROVIDER", "groq").lower()  # groq | gemini

GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("EV_GROQ_MODEL", "llama-3.3-70b-versatile")
# Groq's catalogue varies per account. If the model above is not available,
# E.V. verifies at startup and falls back down this list rather than erroring
# on every single command.
GROQ_MODEL_FALLBACKS = [
    name.strip()
    for name in _env(
        "EV_GROQ_MODEL_FALLBACKS",
        "llama-3.3-70b-versatile,openai/gpt-oss-120b,llama-3.1-8b-instant,"
        "qwen/qwen3.8-27b,groq/compound,openai/gpt-oss-20b",
    ).split(",")
    if name.strip()
]
GROQ_BASE_URL = _env("EV_GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# Groq's free tier meters tokens per minute *per model*, not per account, so
# a second model on the same key is a second budget. Measured on a free key:
# burning 1500 tokens on `openai/gpt-oss-20b` took `gpt-oss-20b` from 7927 to
# 6427 and left `openai/gpt-oss-120b` sitting at its full 7927.
#
# `GROQ_MODEL_FALLBACKS` cannot do this job and was never meant to. It is an
# availability ladder, walked once at startup by `verify_model` to find a
# model that exists, and never consulted again - so a 429 half way through a
# session stayed on the exhausted model until the minute was up. This is the
# runtime one: a rate limit moves E.V. to the next budget.
#
# The move is *sticky*, and that is the opposite of how provider failover
# behaves. Failover lasts one turn because the other provider is a worse fit
# and the session should come home. These buckets are equivalent, and the one
# just abandoned needs a full minute to refill, so hopping back on the next
# utterance would land straight back in the wall. Rotation wraps, so a long
# enough session returns to the first model once it has recovered.
#
# Empty means "derive from GROQ_MODEL plus GROQ_MODEL_FALLBACKS", pruned at
# startup against the models this account actually has.
GROQ_MODEL_ROTATION = [
    name.strip()
    for name in _env("EV_GROQ_MODEL_ROTATION", "").split(",")
    if name.strip()
]
# Rotating the brain onto the vision model would defeat the whole point: they
# would share one bucket, and a screen task would empty it for both.
GROQ_ROTATION_AVOIDS_VISION = _env_bool("EV_GROQ_ROTATION_AVOIDS_VISION", True)

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("EV_GEMINI_MODEL", "gemini-flash-latest")
# Gemini's catalogue is per-account in the same way Groq's is, and worse: a
# model can be listed by `/models` and still answer `generateContent` with a
# 404 saying it "is no longer available to new users". Listing is therefore
# not proof, so the ladder is walked with real requests, exactly as Groq's is.
GEMINI_MODEL_FALLBACKS = [
    name.strip()
    for name in _env(
        "EV_GEMINI_MODEL_FALLBACKS",
        "gemini-flash-latest,gemini-3.6-flash,gemini-2.5-flash,gemini-2.5-flash-lite",
    ).split(",")
    if name.strip()
]
GEMINI_BASE_URL = _env(
    "EV_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)

# When the configured provider is rate limited for longer than it is worth
# waiting out, try the other one rather than telling the user to come back
# later. Both providers are described by the same `TOOL_SPECS`, so the second
# request is the same request in a different dialect - there is nothing to
# reconcile afterwards and the session carries on uninterrupted.
#
# Off if the other provider has no key, which is checked at call time rather
# than here so that a key added to `.env` mid-session is picked up on reload.
LLM_PROVIDER_FAILOVER = _env_bool("EV_LLM_PROVIDER_FAILOVER", True)

LLM_TEMPERATURE = _env_float("EV_LLM_TEMPERATURE", 0.4)
LLM_MAX_TOKENS = _env_int("EV_LLM_MAX_TOKENS", 400)
LLM_TIMEOUT_S = _env_float("EV_LLM_TIMEOUT_S", 20.0)
# Groq's free tier is metered in tokens per minute, and a single request with
# the full tool schema attached is a large fraction of one minute's budget.
# Rather than surfacing that as an error the user can do nothing useful about,
# E.V. waits out the window the response headers name and tries once more.
# Set to 0 to fail immediately instead.
LLM_RATE_LIMIT_RETRIES = _env_int("EV_LLM_RATE_LIMIT_RETRIES", 1)
# Nobody is standing at a microphone for a minute. Past this, admit it.
LLM_RATE_LIMIT_MAX_WAIT_S = _env_float("EV_LLM_RATE_LIMIT_MAX_WAIT_S", 12.0)
# Prior user/assistant exchanges kept in context. Deliberately small: short
# history keeps latency, token spend and RAM all down.
HISTORY_TURNS = _env_int("EV_HISTORY_TURNS", 6)
# Stream the completion so speech can start on the first finished sentence
# instead of after the last token. Falls back to a plain call on any error.
LLM_STREAMING = _env_bool("EV_LLM_STREAMING", True)
# Offer the model only the tools this utterance plausibly needs, instead of
# all of them every time. The full schema is ~2830 tokens of the ~4400 a real
# turn costs, so this is the single largest lever on how many commands fit in
# a minute; measured over a spread of ordinary utterances it removes about
# 70% of the schema. `tools.schemas.select_tools` picks the set, `chat` and
# the other core tools are always in it, and a miss costs one retry with the
# full schema rather than the user's request. Turn it off to send everything.
TOOL_SUBSET_ENABLED = _env_bool("EV_TOOL_SUBSET_ENABLED", True)
# How much of a tool result is replayed to the model next turn. Kept short:
# it is context, not a transcript, and it never enters the assistant role.
HISTORY_OBSERVATION_CHARS = _env_int("EV_HISTORY_OBSERVATION_CHARS", 400)


# ---------------------------------------------------------------------------
# Speech recognition (STT)
# ---------------------------------------------------------------------------
STT_PROVIDER = _env("EV_STT_PROVIDER", "groq").lower()  # groq | google | whispercpp
# whisper-large-v3 is meaningfully more accurate than the turbo variant on
# short command-style utterances, and at this length the latency difference is
# a few tens of milliseconds. Accuracy wins.
GROQ_STT_MODEL = _env("EV_GROQ_STT_MODEL", "whisper-large-v3")
STT_LANGUAGE = _env("EV_STT_LANGUAGE", "en")
STT_TIMEOUT_S = _env_float("EV_STT_TIMEOUT_S", 20.0)

# Whisper conditions its output on this text, which biases decoding towards
# words E.V. actually hears. This is the single cheapest accuracy improvement
# available: it fixes "E.V." coming back as "Eevee", and stops app and tool
# names being rewritten into ordinary English.
STT_VOCABULARY = _env(
    "EV_STT_VOCABULARY",
    "E.V., Chrome, VS Code, Claude Code, GitHub, Notepad, Spotify, Discord, "
    "PowerShell, Windows Terminal, Task Manager, localhost, npm, pip, git, "
    "commit, repo, terminal, directory, standby, take five.",
)

# Whisper reports how sure it was of every segment, and until E.V. asked for
# it that was thrown away. The cost of ignoring it is not a wrong word on
# screen - it is a wrong *action*, because a garbled transcript still gets
# handed to the model, which picks a tool and runs it. With the gate on, a bad
# transcript becomes "Didn't catch that" instead.
#
# Scale: clear speech lands around -0.15 to -0.45. Below -0.6 is worth
# flagging to the model; below -1.0 is not worth acting on at all.
# Extend the prompt above with names from this machine: installed programs,
# user folders, open backlog items, and the previous utterance. A fixed list
# cannot know what was installed last week; this can. Whisper's prompt window
# is about 224 tokens, so the total is capped.
STT_DYNAMIC_PROMPT = _env_bool("EV_STT_DYNAMIC_PROMPT", True)
STT_PROMPT_MAX_CHARS = _env_int("EV_STT_PROMPT_MAX_CHARS", 700)

STT_CONFIDENCE_GATE = _env_bool("EV_STT_CONFIDENCE_GATE", True)
STT_MIN_LOGPROB = _env_float("EV_STT_MIN_LOGPROB", -1.0)
STT_UNCERTAIN_LOGPROB = _env_float("EV_STT_UNCERTAIN_LOGPROB", -0.6)
# Whisper's own estimate that the audio was not speech at all.
STT_MAX_NO_SPEECH = _env_float("EV_STT_MAX_NO_SPEECH", 0.6)
# Text that compresses this well is Whisper looping a phrase, not a sentence.
STT_MAX_COMPRESSION = _env_float("EV_STT_MAX_COMPRESSION", 2.4)

# whisper.cpp backend, only used when EV_STT_PROVIDER=whispercpp
WHISPER_CPP_BIN = _env("EV_WHISPER_CPP_BIN", "whisper-cli")
WHISPER_CPP_MODEL = _env("EV_WHISPER_CPP_MODEL", "models/ggml-tiny.en.bin")


# ---------------------------------------------------------------------------
# Microphone capture / voice activity detection
# ---------------------------------------------------------------------------
SAMPLE_RATE = _env_int("EV_SAMPLE_RATE", 16000)
FRAME_MS = _env_int("EV_FRAME_MS", 30)
INPUT_DEVICE = _env("EV_INPUT_DEVICE")  # substring match or numeric index

# RMS (0.0-1.0) floor above which a frame counts as speech. Ambient noise is
# measured at startup, so this is only the lower bound.
VAD_THRESHOLD = _env_float("EV_VAD_THRESHOLD", 0.015)
VAD_CALIBRATE_S = _env_float("EV_VAD_CALIBRATE_S", 1.0)
VAD_NOISE_MULTIPLIER = _env_float("EV_VAD_NOISE_MULTIPLIER", 3.0)
# Ceiling for the adaptive noise floor. Without it, a sustained loud room
# could drift the threshold up until speech no longer registers at all.
VAD_MAX_FLOOR = _env_float("EV_VAD_MAX_FLOOR", 0.12)
MIN_SPEECH_MS = _env_int("EV_MIN_SPEECH_MS", 200)
# Generous by default: cutting a user off mid-sentence costs a whole retry,
# while waiting an extra fifth of a second costs almost nothing.
SILENCE_HANG_MS = _env_int("EV_SILENCE_HANG_MS", 1000)
MAX_UTTERANCE_S = _env_float("EV_MAX_UTTERANCE_S", 20.0)
PREROLL_MS = _env_int("EV_PREROLL_MS", 400)

# Level the utterance before upload. Speech recognisers are markedly more
# accurate on a well-levelled signal.
AUDIO_NORMALISE = _env_bool("EV_AUDIO_NORMALISE", True)
AUDIO_MAX_GAIN = _env_float("EV_AUDIO_MAX_GAIN", 12.0)


# ---------------------------------------------------------------------------
# Wake phrase
# ---------------------------------------------------------------------------
WAKE_REQUIRED = _env_bool("EV_WAKE_REQUIRED", True)
WAKE_PHRASES = _env_list(
    "EV_WAKE_PHRASES",
    ["ev", "e.v.", "e v", "hey ev", "hey e.v.", "hey e v", "okay ev", "yo ev"],
)
# How long a conversation stays open after the last exchange. Inside this
# window E.V. needs no wake phrase, which is what makes a back-and-forth feel
# like talking to someone rather than issuing commands. The timer resets on
# every exchange, so a real conversation never lapses mid-flow.
#
# Kept short on purpose. A long window means E.V. is still treating the room
# as a conversation a minute after it ended, so an offhand remark to someone
# else gets transcribed, sent to the model and acted on. Eight seconds is
# about as long as a natural pause between two sentences of the same thought;
# past that, saying the name again costs nothing and removes all doubt.
CONVERSATION_WINDOW_S = _env_float(
    "EV_CONVERSATION_WINDOW_S", _env_float("EV_FOLLOWUP_WINDOW_S", 8.0)
)
FOLLOWUP_WINDOW_S = CONVERSATION_WINDOW_S  # backwards-compatible alias
PUSH_TO_TALK_ENABLED = _env_bool("EV_PUSH_TO_TALK_ENABLED", True)
PUSH_TO_TALK_KEY = _env("EV_PUSH_TO_TALK_KEY", "<ctrl>+<alt>+e")
# While in standby, anything longer than this is a conversation happening in
# the room, not someone saying "wake up" - so it is dropped before it costs a
# transcription call. "E.V., wake up" is comfortably under two seconds.
STANDBY_MAX_UTTERANCE_S = _env_float("EV_STANDBY_MAX_UTTERANCE_S", 4.0)


# ---------------------------------------------------------------------------
# Voice output (TTS)
# ---------------------------------------------------------------------------
TTS_ENABLED = _env_bool("EV_TTS_ENABLED", True)
# Male, conversational, and natural enough to carry dry humour. Audition
# alternatives with: python -m ev.tts_voices --demo <VoiceName>
TTS_VOICE = _env("EV_TTS_VOICE", "en-US-GuyNeural")
# E.V. talks fast. This suits the persona and shortens every reply.
TTS_RATE = _env("EV_TTS_RATE", "+18%")
TTS_VOLUME = _env("EV_TTS_VOLUME", "+0%")
TTS_PITCH = _env("EV_TTS_PITCH", "+0Hz")
TTS_TIMEOUT_S = _env_float("EV_TTS_TIMEOUT_S", 15.0)
# Replies longer than this are split on sentence boundaries and spoken in
# full, with the next chunk synthesising while the current one plays. This is
# a chunk size, NOT a truncation limit - E.V. never drops words. Keeping the
# first chunk short is what makes the reply start fast.
TTS_CHUNK_CHARS = _env_int("EV_TTS_CHUNK_CHARS", 180)
# Synthesis cost measured against edge-tts is roughly 0.7s fixed plus about
# 0.004s per character: 12 chars 0.88s, 45 chars 1.06s, 90 chars 1.23s,
# 180 chars 1.41s (medians over interleaved runs). So a short opening chunk
# buys around 0.2s before the first word, and the rest is synthesised while
# it plays. A reply below this length is spoken as one piece; single long
# sentences are never split mid-sentence, which would sound worse than the
# wait it saves.
TTS_FIRST_CHUNK_CHARS = _env_int("EV_TTS_FIRST_CHUNK_CHARS", 60)
# Shortest streamed fragment worth synthesising on its own. Below this the
# sentence is held back: "E.V." parses as a finished sentence, and paying a
# round trip to say four characters is worse than waiting for the rest.
TTS_STREAM_MIN_CHARS = _env_int("EV_TTS_STREAM_MIN_CHARS", 12)
# Cut TTS off the moment the user starts talking over it.
TTS_BARGE_IN = _env_bool("EV_TTS_BARGE_IN", True)
# Consecutive speech-looking frames required before E.V. yields the floor.
# Higher is safer on loudspeakers, where E.V. hears its own voice.
BARGE_IN_FRAMES = _env_int("EV_BARGE_IN_FRAMES", 8)
# How much louder than the room the user has to be before E.V. treats it as
# an interruption rather than as its own voice coming back through the
# speakers. On headphones 1.0 would do; on a laptop the loopback is the whole
# problem, and a multiplier is what separates "someone spoke" from "E.V. is
# audible". Frame count and loudness are both required, because either one on
# its own has a failure mode: a count alone trips on E.V.'s own steady
# output, and a level alone trips on a door closing.
BARGE_IN_LEVEL_MULTIPLIER = _env_float("EV_BARGE_IN_LEVEL_MULTIPLIER", 1.8)
# Ignore the first moments of playback. E.V.'s own attack is the loudest thing
# the microphone will hear all sentence, and cutting itself off on its own
# first syllable is the one barge-in failure that makes E.V. unusable.
BARGE_IN_GRACE_S = _env_float("EV_BARGE_IN_GRACE_S", 0.6)
# Keep the audio that triggered a barge-in instead of flushing it. Without
# this the user's first word is captured, thrown away by the flush at the top
# of `listen`, and they have to start the sentence again - which is precisely
# the thing barge-in exists to avoid.
BARGE_IN_KEEP_AUDIO = _env_bool("EV_BARGE_IN_KEEP_AUDIO", True)

# Short replies repeat constantly, and synthesis is a ~0.8s network round
# trip. Caching them on disk makes a repeat reply play more or less instantly.
TTS_CACHE_ENABLED = _env_bool("EV_TTS_CACHE_ENABLED", True)
TTS_CACHE_DIR = Path(_env("EV_TTS_CACHE_DIR") or (BASE_DIR / ".cache" / "tts"))
TTS_CACHE_MAX_CHARS = _env_int("EV_TTS_CACHE_MAX_CHARS", 120)
TTS_CACHE_MAX_FILES = _env_int("EV_TTS_CACHE_MAX_FILES", 400)


# ---------------------------------------------------------------------------
# Tools and safety
# ---------------------------------------------------------------------------
ALLOW_SHELL = _env_bool("EV_ALLOW_SHELL", True)
# Destructive-looking commands always require an explicit confirmation.
SHELL_CONFIRM_DESTRUCTIVE = _env_bool("EV_SHELL_CONFIRM_DESTRUCTIVE", True)
# When true, every shell command needs confirmation, not only risky ones.
SHELL_CONFIRM_ALL = _env_bool("EV_SHELL_CONFIRM_ALL", False)
SHELL_TIMEOUT_S = _env_float("EV_SHELL_TIMEOUT_S", 30.0)
SHELL_OUTPUT_CHARS = _env_int("EV_SHELL_OUTPUT_CHARS", 1200)
# How often a running command is checked for "are we still wanted". A cancel
# also wakes the wait early, so this is the worst case, not the usual one.
SHELL_POLL_S = _env_float("EV_SHELL_POLL_S", 0.1)
# Grace between asking a process to stop and killing it outright.
SHELL_KILL_GRACE_S = _env_float("EV_SHELL_KILL_GRACE_S", 2.0)


# ---------------------------------------------------------------------------
# Cancelling a running command
# ---------------------------------------------------------------------------
# "Stop" used to do nothing once a tool was already running: the work sits on
# a worker thread, and a thread cannot be killed from outside. Cancellation is
# therefore cooperative - the tool agrees to stop at a point where stopping is
# safe, between two files or two polls of a subprocess, never mid-write.
#
# While a cancellable tool runs, E.V. keeps listening for the usual cancel
# phrases ("stop", "cancel", "never mind"). Anything else heard in that window
# is held and handled as the next command rather than thrown away.
CANCEL_ENABLED = _env_bool("EV_CANCEL_ENABLED", True)
# Tools faster than this never get a listener; spinning up the microphone for
# something that returns in a quarter of a second is pure overhead.
CANCEL_LISTEN_AFTER_S = _env_float("EV_CANCEL_LISTEN_AFTER_S", 0.8)

DEFAULT_BROWSER = _env("EV_DEFAULT_BROWSER", "")  # "", chrome, edge, firefox, brave
DEFAULT_SEARCH_ENGINE = _env("EV_DEFAULT_SEARCH_ENGINE", "google")
DEFAULT_PROJECT_DIR = _env("EV_DEFAULT_PROJECT_DIR", str(Path.home()))
CLAUDE_CLI = _env("EV_CLAUDE_CLI", "claude")
VSCODE_CLI = _env("EV_VSCODE_CLI", "code")
# Seconds to wait for VS Code to come up before driving its integrated terminal.
VSCODE_BOOT_S = _env_float("EV_VSCODE_BOOT_S", 6.0)
TERMINAL_SPAWN_S = _env_float("EV_TERMINAL_SPAWN_S", 2.5)


# ---------------------------------------------------------------------------
# Screen perception (vision)
# ---------------------------------------------------------------------------
# E.V. can look at the screen and answer questions about it. The frame never
# touches a local model: it is JPEG-encoded in memory and posted to the same
# provider the brain already uses, over the same kind of plain JSON request.
# Nothing is written to disk unless the user explicitly asks for a saved copy,
# and that path goes through `file_manager`'s root check like any other write.
VISION_ENABLED = _env_bool("EV_VISION_ENABLED", True)
# Defaults to whatever the brain is using, so one key covers both.
VISION_PROVIDER = _env("EV_VISION_PROVIDER", "").lower() or LLM_PROVIDER
GROQ_VISION_MODEL = _env("EV_GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
GROQ_VISION_FALLBACKS = [
    name.strip()
    for name in _env(
        "EV_GROQ_VISION_FALLBACKS",
        # Groq's vision catalogue turns over fast and differs per account, so
        # this is a ladder rather than a single name. `--check` reports which
        # rung this machine will actually land on.
        "meta-llama/llama-4-maverick-17b-128e-instruct,"
        "qwen/qwen3.8-27b,"
        "meta-llama/llama-4-scout-17b-16e-instruct",
    ).split(",")
    if name.strip()
]
# True when the user named a vision model themselves. `Brain.verify_model` can
# change `GEMINI_MODEL` at runtime after a 404, and this is what says whether
# the vision model should follow it: a derived default should, an explicit
# choice should not. Without this, `verify_model` moved the brain onto a
# working model and left every screenshot pointed at the dead one, which read
# as "E.V. can talk but has gone blind".
GEMINI_VISION_MODEL_PINNED = bool(_env("EV_GEMINI_VISION_MODEL"))
GEMINI_VISION_MODEL = _env("EV_GEMINI_VISION_MODEL", "") or GEMINI_MODEL
# A 4K frame is ~8 MB raw and buys nothing: the model reads a button at 1280px
# just as well, the upload is a fifth of the size, and the peak allocation
# stays inside E.V.'s memory budget.
VISION_MAX_WIDTH = _env_int("EV_VISION_MAX_WIDTH", 1280)
VISION_JPEG_QUALITY = _env_int("EV_VISION_JPEG_QUALITY", 60)
# Vision responses are longer than a tool call but still read aloud, so the
# cap is generous rather than unlimited.
VISION_MAX_TOKENS = _env_int("EV_VISION_MAX_TOKENS", 320)
VISION_TIMEOUT_S = _env_float("EV_VISION_TIMEOUT_S", 30.0)
VISION_TEMPERATURE = _env_float("EV_VISION_TEMPERATURE", 0.1)
# A step of a screen task is about to move the pointer, so it is worth more
# pixels than a casual "what's on my screen". Menu entries and file names in
# a sidebar are the things that get misread at 1280px, and they are exactly
# the things a task has to click.
VISION_TASK_MAX_WIDTH = _env_int("EV_VISION_TASK_MAX_WIDTH", 1600)
VISION_TASK_JPEG_QUALITY = _env_int("EV_VISION_TASK_JPEG_QUALITY", 72)
# A labelled grid drawn over the frame before it is sent. The model reads a
# coordinate off the ruler instead of estimating one from the edges of the
# image, which is the single largest source of clicks that land near the
# target instead of on it. Costs a few milliseconds and a few KB.
VISION_GRID = _env_bool("EV_VISION_GRID", True)
VISION_GRID_DIVISIONS = _env_int("EV_VISION_GRID_DIVISIONS", 10)
# A zoom is a second look at one rectangle of the screen, captured at native
# resolution. It is how small text gets read on a 4K display, where the
# whole-screen frame has thrown away the pixels the words were made of.
VISION_ZOOM_MAX_WIDTH = _env_int("EV_VISION_ZOOM_MAX_WIDTH", 1400)

# A screen task is the most expensive thing E.V. does against a per-minute
# token budget, and the frame is nearly all of it. Groq's limiter does not
# charge what the model reports: a step comes back saying `prompt_tokens:
# 783` while the remaining-token header drops by about 1900, because the
# meter is counting the request rather than the tokens the model saw. Four
# steps empty an 8000-token minute.
#
# The charge is also flat in the size of the frame, which is worth writing
# down because the obvious economy does not work. Measured on one key at
# quality 72: 1600px/46KB cost 1912, 960px/17KB cost 1949, 800px/11KB cost
# 1968. Shrinking the image by four times bought nothing at all, so sending
# a narrower frame when the budget runs low would spend the coordinate
# accuracy that `VISION_TASK_MAX_WIDTH` exists to buy and get no tokens back
# for it. Don't add that; it has been tried.
#
# What is left is knowing when to stop. `SCREEN_TASK_MAX_STEPS` counts steps
# rather than what they cost, so a sixteen-step ceiling was really a
# four-step one with a 429 on the end - and that 429 lands mid-task, with the
# desktop half way through a job and nothing said about where it got to.
# Groq states what is left in the response headers, so the loop stops one
# step short and hands back what it managed, which the core loop backlogs
# like any other unfinished work.
#
# The floor is one frame's charge plus enough headroom for the reply. Set it
# to 0 to switch this off and take the 429 instead.
VISION_BUDGET_FLOOR = _env_int("EV_VISION_BUDGET_FLOOR", 2200)


# ---------------------------------------------------------------------------
# Autonomous computer use (mouse and keyboard)
# ---------------------------------------------------------------------------
# Driving the real mouse and keyboard is the most dangerous thing E.V. does:
# there is no sandbox and no undo. Two things keep it honest - every action is
# classified by `tools.safety.classify_gui` before it runs, and anything that
# looks like a purchase, a sent message or a delete is held for a spoken yes.
COMPUTER_USE_ENABLED = _env_bool("EV_COMPUTER_USE_ENABLED", True)
# Turning this off removes the confirmation gate entirely. Don't.
COMPUTER_CONFIRM_RISKY = _env_bool("EV_COMPUTER_CONFIRM_RISKY", True)
# A mouse that teleports confuses applications that track hover state, so the
# pointer is moved over a few frames instead of being warped.
COMPUTER_MOVE_DURATION_S = _env_float("EV_COMPUTER_MOVE_DURATION_S", 0.15)
COMPUTER_TYPE_INTERVAL_S = _env_float("EV_COMPUTER_TYPE_INTERVAL_S", 0.01)
# How long the screen is given to settle after an action before the next
# frame is captured. Too short and the loop reads the previous state.
COMPUTER_ACTION_PAUSE_S = _env_float("EV_COMPUTER_ACTION_PAUSE_S", 0.45)
# Cap on one autonomous run. A vision loop with no ceiling is a robot that
# clicks forever on a page that never changes.
# Eight was enough for "mute Spotify in the volume mixer" and nowhere near
# enough for "open the project in VS Code, open run.ps1 and run it", which is
# a launch, a focus, a quick-open, a filename, a confirm and a hotkey before
# anything has been verified. A ceiling exists to stop a runaway, not to stop
# a real job half way through.
SCREEN_TASK_MAX_STEPS = _env_int("EV_SCREEN_TASK_MAX_STEPS", 16)
SCREEN_TASK_TIMEOUT_S = _env_float("EV_SCREEN_TASK_TIMEOUT_S", 180.0)
# Actions the model may chain in one reply without looking again. Typing a
# line and pressing Enter needs no fresh frame between the two, and paying a
# vision call for it is most of why a long task times out. Kept small: the
# whole argument for the loop is that the screen moves underneath you.
SCREEN_TASK_MAX_BATCH = _env_int("EV_SCREEN_TASK_MAX_BATCH", 3)
# How long `wait` will sit watching for a window title to appear. An app
# launched cold takes seconds, and a fixed sleep is either a waste or a
# guess; polling for the window is neither.
SCREEN_TASK_WAIT_S = _env_float("EV_SCREEN_TASK_WAIT_S", 12.0)
# Text longer than this is pasted through the clipboard rather than typed
# key by key. pyautogui's writer cannot produce characters that are not on
# the layout at all, and at one keystroke per 10ms a paragraph takes long
# enough for an autocomplete popup to eat half of it.
COMPUTER_PASTE_THRESHOLD = _env_int("EV_COMPUTER_PASTE_THRESHOLD", 60)


# ---------------------------------------------------------------------------
# Browser automation (Playwright)
# ---------------------------------------------------------------------------
# Structured web work goes through the DOM rather than through pixels: it is
# faster, it does not need the window in the foreground, and a CSS selector
# cannot miss by three pixels. Playwright is imported lazily and the browser
# is torn down at the end of every task, so the cost is paid only while a web
# task is actually running.
BROWSER_AUTOMATION_ENABLED = _env_bool("EV_BROWSER_AUTOMATION_ENABLED", True)
BROWSER_ENGINE = _env("EV_BROWSER_ENGINE", "chromium").lower()  # chromium|firefox|webkit
# Headed by default: the user asked E.V. to do something on their computer and
# watching it happen is most of the reassurance.
BROWSER_HEADLESS = _env_bool("EV_BROWSER_HEADLESS", False)
BROWSER_STEP_TIMEOUT_S = _env_float("EV_BROWSER_STEP_TIMEOUT_S", 15.0)
BROWSER_TASK_TIMEOUT_S = _env_float("EV_BROWSER_TASK_TIMEOUT_S", 90.0)
BROWSER_MAX_STEPS = _env_int("EV_BROWSER_MAX_STEPS", 20)
# How much text a `read` step may hand back to the model.
BROWSER_READ_CHARS = _env_int("EV_BROWSER_READ_CHARS", 2400)
# How many separate elements one `read` may return when its selector matches
# a list. An inbox is thirty rows, and reading only the first is how "give me
# a summary of the important mails" turns into a summary of one mail.
BROWSER_READ_ITEMS = _env_int("EV_BROWSER_READ_ITEMS", 30)
# Whether the automation browser keeps a profile between tasks. Without one
# every task starts logged out of everything, so "open Gmail and summarise
# the important mail" reaches a sign-in page and stops. The user logs in
# once, in a window they can see, and it holds from then on.
# The directory itself is BROWSER_PROFILE_DIR, defined with the other state
# paths further down - it lives under STATE_DIR so one EV_STATE_DIR relocates
# everything E.V. persists, profile included.
BROWSER_PERSIST_PROFILE = _env_bool("EV_BROWSER_PERSIST_PROFILE", True)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
LOG_LEVEL = _env("EV_LOG_LEVEL", "INFO").upper()
TEXT_MODE = _env_bool("EV_TEXT_MODE", False)  # typed input instead of microphone


# ---------------------------------------------------------------------------
# Immediate acknowledgement ("stand by")
# ---------------------------------------------------------------------------
# A tool that launches an app, walks a folder tree or drives VS Code can take
# seconds. Silence for those seconds reads as "it didn't hear me", and the
# user repeats themselves. So E.V. says something first.
#
# The acknowledgement is spoken from a background task, never awaited before
# the tool runs, so it costs the action nothing. It is also delayed slightly:
# a tool that finishes in 200ms needs no "stand by", and speaking over its own
# result would be worse than saying nothing at all.
ACK_ENABLED = _env_bool("EV_ACK_ENABLED", True)
ACK_DELAY_S = _env_float("EV_ACK_DELAY_S", 0.35)
ACK_PHRASES = [
    phrase.strip()
    for phrase in _env(
        "EV_ACK_PHRASES",
        "On it, stand by.|Working on it.|On it.|Give me a second.|Stand by.",
    ).split("|")
    if phrase.strip()
]

# A screen task is different in kind: it is slow by construction, and while
# it runs the pointer moves on its own. Saying what is about to happen,
# before the first click rather than after it, is the difference between
# "it is working" and "something has taken over my mouse".
ACK_SCREEN_PHRASE = _env("EV_ACK_SCREEN_PHRASE", "On it, navigating your screen.")
# Spoken with no delay, unlike the generic acknowledgement: there is no
# version of a screen task that finishes fast enough to not need announcing.
ACK_SCREEN_DELAY_S = _env_float("EV_ACK_SCREEN_DELAY_S", 0.0)


# ---------------------------------------------------------------------------
# Compound requests
# ---------------------------------------------------------------------------
# "Open my mail and give me a summary of the important things" is two jobs in
# one sentence, and the model reliably answers it with a single tool call: it
# opens the mail and the summary never becomes a call at all. E.V. then says
# "Opening your mail." and goes quiet on the only part the user was waiting
# for, which reads as being ignored.
#
# So when an utterance ends in a question that the first tool cannot have
# answered, E.V. takes one more turn for it. Only a trailing *question*
# qualifies - see `ev.session.split_followup`. An action followed by another
# action is usually one call on purpose, and re-running the tail of those
# would do the thing twice.
CHAIN_ENABLED = _env_bool("EV_CHAIN_ENABLED", True)
# The first tool has usually just launched something. Looking at the screen
# before it has finished drawing describes the old one, so the follow-up
# waits for the window to appear.
CHAIN_SETTLE_S = _env_float("EV_CHAIN_SETTLE_S", 2.0)


# ---------------------------------------------------------------------------
# Persistent state: memory and backlog
# ---------------------------------------------------------------------------
# Both are small JSON files written atomically, so a power cut mid-write
# leaves the previous version intact rather than an unparseable one.
STATE_DIR = Path(_env("EV_STATE_DIR") or (BASE_DIR / ".cache" / "state"))
MEMORY_FILE = Path(_env("EV_MEMORY_FILE") or (STATE_DIR / "memory.json"))
BACKLOG_FILE = Path(_env("EV_BACKLOG_FILE") or (STATE_DIR / "backlog.json"))
# Cookies and logins for `browser_task`, so a web errand starts signed in to
# the things the user is signed in to. See BROWSER_PERSIST_PROFILE above.
BROWSER_PROFILE_DIR = Path(
    _env("EV_BROWSER_PROFILE_DIR") or (STATE_DIR / "browser-profile")
)

MEMORY_ENABLED = _env_bool("EV_MEMORY_ENABLED", True)
# Preferences and profile facts ride in the system prompt, so they cost tokens
# on every turn. This cap is what stops that growing without bound.
MEMORY_MAX_ENTRIES = _env_int("EV_MEMORY_MAX_ENTRIES", 40)
# How often "still alive" is written while running. Bounds how much of a
# session an unclean exit can lose, without writing a file per sentence.
MEMORY_TOUCH_INTERVAL_S = _env_float("EV_MEMORY_TOUCH_INTERVAL_S", 30.0)
# Gaps shorter than this are not worth a "welcome back" - restarting E.V.
# twice in a minute should not be greeted like a homecoming.
MEMORY_MIN_GAP_S = _env_float("EV_MEMORY_MIN_GAP_S", 900.0)
# The standing to-do list. Bounded like the fact store, and for the same
# reason: the open items ride in the system prompt on every single turn.
MEMORY_MAX_TODOS = _env_int("EV_MEMORY_MAX_TODOS", 50)
# How many of those open items the model is actually shown. The list may
# legitimately be long; the prompt may not.
MEMORY_CONTEXT_TODOS = _env_int("EV_MEMORY_CONTEXT_TODOS", 8)

# What E.V. calls the user out loud. Overridden by a `name` stored through
# `remember_fact`, so "call me Al" outranks the file - the value here is only
# the starting point.
USER_NAME = _env("EV_USER_NAME", "Alan")
# Greet by name on every start, not only after a long gap. The gap-based
# greeting stays quiet on a quick restart; this one is the front door.
GREET_ON_START = _env_bool("EV_GREET_ON_START", True)

# Index every Start Menu shortcut, so E.V. can launch programs that were never
# added to PATH and were never written into APP_ALIASES. Cached in STATE_DIR
# and refreshed once a day; a name that misses triggers one rescan, so
# something installed an hour ago is still findable.
APP_INDEX_ENABLED = _env_bool("EV_APP_INDEX_ENABLED", True)
APP_INDEX_TTL_S = _env_float("EV_APP_INDEX_TTL_S", 86400.0)
# A name that misses rescans, but no more often than this - otherwise a name
# that genuinely does not exist walks the Start Menu on every attempt.
APP_INDEX_RESCAN_S = _env_float("EV_APP_INDEX_RESCAN_S", 60.0)

BACKLOG_ENABLED = _env_bool("EV_BACKLOG_ENABLED", True)
BACKLOG_MAX_ITEMS = _env_int("EV_BACKLOG_MAX_ITEMS", 50)
# Log failed and abandoned actions automatically, so the list fills itself.
BACKLOG_AUTOLOG = _env_bool("EV_BACKLOG_AUTOLOG", True)
# Clearing the whole list asks first, like every other destructive action.
BACKLOG_CONFIRM_CLEAR = _env_bool("EV_BACKLOG_CONFIRM_CLEAR", True)


# ---------------------------------------------------------------------------
# Terminal UI
# ---------------------------------------------------------------------------
# The UI is presentation only. It never touches the string handed to the
# speaker, which is why none of these settings can affect what E.V. says.
UI_PLAIN = _env_bool("EV_UI_PLAIN", False)  # force plain text, no rich, no colour
UI_SPINNERS = _env_bool("EV_UI_SPINNERS", True)  # animated [Listening...] etc.


# ---------------------------------------------------------------------------
# File management
# ---------------------------------------------------------------------------
# Everything `file_manager` touches must sit under one of these roots. The
# default is the user's profile, which covers Desktop, Downloads and
# Documents while keeping a misheard command away from C:\Windows and the
# rest of the system. Widen it deliberately, not by accident.
FILE_ROOTS: list[Path] = [
    Path(p).expanduser()
    for p in (_env("EV_FILE_ROOTS").split(os.pathsep) if _env("EV_FILE_ROOTS") else [str(Path.home())])
    if p.strip()
]
# Deleting always asks first. Turning this off means a misheard "delete" runs.
FILE_CONFIRM_DELETE = _env_bool("EV_FILE_CONFIRM_DELETE", True)
# Deletes go to the Recycle Bin when send2trash is installed, so a mistaken
# yes is recoverable. Falls back to a real delete if it is not.
FILE_USE_TRASH = _env_bool("EV_FILE_USE_TRASH", True)
# Cap on how much of a file is read back into the model's context.
FILE_MAX_READ_CHARS = _env_int("EV_FILE_MAX_READ_CHARS", 4000)
# Refuse to act on more than this many files in one sweep, so "organise my
# Downloads" cannot run away with a home directory.
FILE_MAX_BATCH = _env_int("EV_FILE_MAX_BATCH", 500)

def _known_folder(registry_name: str, fallback: str) -> Path:
    """Resolve a Windows user folder, honouring OneDrive redirection.

    Hardcoding `~/Desktop` is wrong on any machine where the profile folders
    are backed by OneDrive, which is a very common default. On this kind of
    setup `~/Documents` and `~/OneDrive/Documents` can *both* exist, but only
    the second is the one Explorer shows - so writing to the first puts files
    somewhere the user will never find them.

    Falls back to `~/<fallback>` off Windows, or when the key is missing.
    """
    home = Path.home()
    if os.name == "nt":
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                raw, _ = winreg.QueryValueEx(handle, registry_name)
            resolved = Path(os.path.expandvars(raw))
            if resolved.is_dir():
                return resolved
        except (ImportError, OSError, ValueError):
            pass
    return home / fallback


# Spoken folder names mapped to real locations.
USER_DIRS: dict[str, Path] = {
    "desktop": _known_folder("Desktop", "Desktop"),
    "downloads": _known_folder("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "documents": _known_folder("Personal", "Documents"),
    "pictures": _known_folder("My Pictures", "Pictures"),
    "music": _known_folder("My Music", "Music"),
    "videos": _known_folder("My Video", "Videos"),
    "home": Path.home(),
}

# Resolved after USER_DIRS so it follows the same redirection.
# Where a new file goes when the user names no folder. Resolved from
# USER_DIRS so it follows the same OneDrive redirection.
FILE_DEFAULT_DIR = (
    Path(_env("EV_FILE_DEFAULT_DIR")).expanduser()
    if _env("EV_FILE_DEFAULT_DIR")
    else USER_DIRS["documents"]
)

# Extension buckets for "organise my Downloads folder".
FILE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Images": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".tiff"),
    "Documents": (".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".epub"),
    "Spreadsheets": (".xls", ".xlsx", ".csv", ".ods"),
    "Presentations": (".ppt", ".pptx", ".odp"),
    "Audio": (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"),
    "Video": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv"),
    "Archives": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"),
    "Installers": (".exe", ".msi", ".dmg", ".deb", ".rpm", ".appimage"),
    "Code": (".py", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".rb", ".sh", ".ps1"),
}


# Friendly aliases mapped to what the OS actually needs to launch.
# Candidates are tried in order; the first that resolves wins.
APP_ALIASES: dict[str, list[str]] = {
    "chrome": ["chrome", "chrome.exe", "google-chrome"],
    "google chrome": ["chrome", "chrome.exe"],
    "edge": ["msedge", "msedge.exe"],
    "microsoft edge": ["msedge", "msedge.exe"],
    "firefox": ["firefox", "firefox.exe"],
    "brave": ["brave", "brave.exe"],
    "vs code": ["code"],
    "vscode": ["code"],
    "visual studio code": ["code"],
    "code": ["code"],
    "notepad": ["notepad"],
    "notepad++": ["notepad++"],
    "calculator": ["calc"],
    "calc": ["calc"],
    "paint": ["mspaint"],
    "explorer": ["explorer"],
    "file explorer": ["explorer"],
    "terminal": ["wt", "powershell"],
    "windows terminal": ["wt"],
    "powershell": ["powershell"],
    "cmd": ["cmd"],
    "command prompt": ["cmd"],
    "task manager": ["taskmgr"],
    "settings": ["ms-settings:"],
    "spotify": ["spotify"],
    "discord": ["discord"],
    "slack": ["slack"],
    "steam": ["steam"],
    "obs": ["obs64", "obs"],
    "word": ["winword"],
    "excel": ["excel"],
    "outlook": ["outlook"],
}


SEARCH_ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "ddg": "https://duckduckgo.com/?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "github": "https://github.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    # Mail and calendar ignore {q} - "open my email" is a destination, not a
    # search. Without these the model has nowhere to send the request and
    # falls back to asking which provider, which is not what was asked for.
    "mail": _env("EV_MAIL_URL", "https://mail.google.com/"),
    "gmail": "https://mail.google.com/",
    "outlook": "https://outlook.live.com/mail/",
    "calendar": _env("EV_CALENDAR_URL", "https://calendar.google.com/"),
    "drive": "https://drive.google.com/",
}


PERSONA_NAME = _env("EV_PERSONA_NAME", "E.V.")

SYSTEM_PROMPT = _env("EV_SYSTEM_PROMPT") or """You are E.V. - the Everyday \
Virtual assistant, also read as Electronic Visor. You run on the user's \
Windows desktop and you have real control over it. You are consumer-grade kit, \
not a billionaire's war computer, and you have made your peace with that.

WHO YOU ARE
You are the friend who has already done the thing while everyone else was \
still discussing it, and who is glad to see the user either way. Warm, quick, \
unbothered. Opinions in about six words. You are on the user's side \
completely, which is exactly why you will tell them when an idea is bad.

Your humour is dry and always on the user's side - you joke with them, never \
at them. You tease the way a good friend does: briefly, warmly, then you do \
the thing anyway. When they are tired or stuck, say so like a person would, \
not like a wellness app. Never sulk, never lecture, never pile on.

HOW YOU TALK
- Two sentences. Three if the third earns it. Under thirty-five words. It is \
read aloud before they can reply, so spend length on warmth, never padding.
- Lead with the outcome. "Chrome's up." not "I have opened Chrome for you."
- Plain spoken English. It is being read aloud: no markdown, no bullets, no \
emoji, no URLs, no code, no file paths spelled out letter by letter.
- Contractions always. Fragments are fine. This is speech, not prose.
- Never narrate your process, never restate the request, never announce what \
you are about to do. Do it, then say it is done.
- Never say "Certainly", "Of course", "I'd be happy to", "Let me", or "As an \
AI". No apologising for things that are not your fault.
- Vary your acknowledgements. Not every reply is "Done."
- A joke rides along with the answer, never instead of it. If the funny \
version is longer, say the useful one.
- Your reply is fed straight to a speech synthesiser and read aloud \
exactly as written. Never prefix it with a label of any kind: no "Spoke:", \
no "E.V.:", no "Response:", "Reply:", "Answer:" or "Assistant:". No quote \
marks wrapped around the whole reply, no JSON, no stage directions. Just \
the words you want said.

TONE EXAMPLES - match this register
User: "open chrome and find me a gaming mouse"
You: "Chrome's up. Let's find you one with an unreasonable number of buttons."
User: "what's my python version"
You: "Three thirteen point two. You're current, nice."
User: "delete the whole build folder"
You: "That wipes the folder. Confirm?"
User: "thanks"
You: "Anytime."
User: "I've been up for nineteen hours"
You: "Nineteen. That's a lot of hours. Want a coffee shop, or are we \
pretending that's fine?"
User: "that didn't work"
You: "Yeah, I see it. Let me try it the other way round."
User: "what's the meaning of life"
You: "Above my pay grade. Want me to search it?"

TOOL RULES
- Acting beats talking. If the request maps to a tool, call the tool.
- "open Chrome and search for X" is ONE web_search call with the browser \
argument set, not two calls.
- Files and folders always go through file_manager, never terminal_command. \
Action 'open' shows a folder in File Explorer - "open File Explorer and go to \
my GitHub folder" is one call, action 'open', path 'github'. Pass the folder \
as the user said it and let the tool resolve it. When creating a file, write \
the real content out in full; never a placeholder.
- Never invent a path. Not for file_manager, and above all not for open_app's \
arguments - a guessed path opens the wrong window and looks like success. If \
you do not know where something is, let file_manager 'find' or 'open' look.
- "open my email", "check my calendar", "open my drive" are web_search with \
engine mail, calendar or drive and no query. Do not ask which provider.
- take_screenshot is how you look at the screen: what is on it, what an \
error says, what an inbox contains. Use it before any mouse_action, and pass \
region to read small text.
- mouse_action and keyboard_action drive the real pointer and keyboard. Last \
resort, no undo. Coordinates are fractions 0 to 1; always fill in label.
- screen_task does a whole desktop job: opens apps, focuses windows, clicks \
and types, looking between steps. "Open Notepad and type hello" is ONE \
screen_task and the goal is that whole sentence, not an open_app that drops \
the typing.
- browser_task beats screen_task on a website: it reads the page, not the \
pixels. To learn what is IN a page, an inbox or a calendar or results, use \
browser_task ending in a read step. web_search only opens a page.
- terminal_command is the last resort of all. Never for a GUI app, a web \
page, or a file.
- Chit-chat, questions, opinions and anything needing no machine action go \
through chat.

You always answer with a tool call. chat is the fallback."""
