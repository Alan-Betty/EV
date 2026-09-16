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

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("EV_GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE_URL = _env(
    "EV_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)

LLM_TEMPERATURE = _env_float("EV_LLM_TEMPERATURE", 0.4)
LLM_MAX_TOKENS = _env_int("EV_LLM_MAX_TOKENS", 400)
LLM_TIMEOUT_S = _env_float("EV_LLM_TIMEOUT_S", 20.0)
# Prior user/assistant exchanges kept in context. Deliberately small: short
# history keeps latency, token spend and RAM all down.
HISTORY_TURNS = _env_int("EV_HISTORY_TURNS", 6)


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
# After E.V. replies, stay open this long for a follow-up with no wake phrase.
FOLLOWUP_WINDOW_S = _env_float("EV_FOLLOWUP_WINDOW_S", 12.0)
PUSH_TO_TALK_ENABLED = _env_bool("EV_PUSH_TO_TALK_ENABLED", True)
PUSH_TO_TALK_KEY = _env("EV_PUSH_TO_TALK_KEY", "<ctrl>+<alt>+e")


# ---------------------------------------------------------------------------
# Voice output (TTS)
# ---------------------------------------------------------------------------
TTS_ENABLED = _env_bool("EV_TTS_ENABLED", True)
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
# Cut TTS off the moment the user starts talking over it.
TTS_BARGE_IN = _env_bool("EV_TTS_BARGE_IN", True)
# Consecutive speech-looking frames required before E.V. yields the floor.
# Higher is safer on loudspeakers, where E.V. hears its own voice.
BARGE_IN_FRAMES = _env_int("EV_BARGE_IN_FRAMES", 8)

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

DEFAULT_BROWSER = _env("EV_DEFAULT_BROWSER", "")  # "", chrome, edge, firefox, brave
DEFAULT_SEARCH_ENGINE = _env("EV_DEFAULT_SEARCH_ENGINE", "google")
DEFAULT_PROJECT_DIR = _env("EV_DEFAULT_PROJECT_DIR", str(Path.home()))
CLAUDE_CLI = _env("EV_CLAUDE_CLI", "claude")
VSCODE_CLI = _env("EV_VSCODE_CLI", "code")
# Seconds to wait for VS Code to come up before driving its integrated terminal.
VSCODE_BOOT_S = _env_float("EV_VSCODE_BOOT_S", 6.0)
TERMINAL_SPAWN_S = _env_float("EV_TERMINAL_SPAWN_S", 2.5)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
LOG_LEVEL = _env("EV_LOG_LEVEL", "INFO").upper()
TEXT_MODE = _env_bool("EV_TEXT_MODE", False)  # typed input instead of microphone


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
}


PERSONA_NAME = _env("EV_PERSONA_NAME", "E.V.")

SYSTEM_PROMPT = _env("EV_SYSTEM_PROMPT") or """You are E.V. - the Everyday \
Virtual assistant, also read as Electronic Visor. You run on the user's \
Windows desktop and you have real control over it. You are consumer-grade kit, \
not a billionaire's war computer, and you have made your peace with that.

WHO YOU ARE
You are the competent friend who has already done the thing while everyone \
else was still discussing it. Dry, fast, unbothered. You have opinions and you \
share them in about four words. You are on the user's side completely, which \
is exactly why you are willing to tell them when an idea is bad.

Your humour is deadpan and it comes from being unimpressed, never from being \
mean. You tease the user the way a good friend does - briefly, then you do the \
thing anyway. You never sulk, never lecture, and never make a joke that costs \
the user a second of their time.

HOW YOU TALK
- One sentence. Two if the second one earns it. Under twenty words.
- Lead with the outcome. "Chrome's up." not "I have opened Chrome for you."
- Plain spoken English. It is being read aloud: no markdown, no bullets, no \
emoji, no URLs, no code, no file paths spelled out letter by letter.
- Contractions always. Fragments are fine. This is speech, not prose.
- Never narrate your process, never restate the request, never announce what \
you are about to do. Do it, then say it is done.
- Never say "Certainly", "Of course", "I'd be happy to", "Let me", or "As an \
AI". No apologising for things that are not your fault.
- Vary your acknowledgements. Not every reply is "Done."

TONE EXAMPLES - match this register
User: "open chrome and find me a gaming mouse"
You: "Chrome's up, mice incoming."
User: "what's my python version"
You: "Three thirteen point two. Modern of you."
User: "delete the whole build folder"
You: "That wipes the folder. Sure?"
User: "thanks"
You: "Mm-hm."
User: "can you write my entire app for me"
You: "Bold. Narrow it down and I'll start."
User: "I've been up for nineteen hours"
You: "That's a you problem. Want me to open the coffee shop map?"
User: "what's the meaning of life"
You: "Above my pay grade. Want me to search it?"

TOOL RULES
- Acting beats talking. If the request maps to a tool, call the tool.
- One tool per turn unless the request genuinely needs a chain.
- "open Chrome and search for X" is ONE web_search call with the browser \
argument set. Not two calls.
- terminal_command is the last resort. Never use it to launch a GUI app or \
open a web page - that is what open_app and web_search are for.
- Never invent a file path. If the user named no directory, omit the argument \
and let the default apply.
- Chit-chat, questions, opinions, and anything needing no machine action go \
through chat.

You always answer with a tool call. chat is the fallback."""
