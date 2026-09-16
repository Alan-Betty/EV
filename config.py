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
# Stream the completion so speech can start on the first finished sentence
# instead of after the last token. Falls back to a plain call on any error.
LLM_STREAMING = _env_bool("EV_LLM_STREAMING", True)
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
CONVERSATION_WINDOW_S = _env_float(
    "EV_CONVERSATION_WINDOW_S", _env_float("EV_FOLLOWUP_WINDOW_S", 75.0)
)
FOLLOWUP_WINDOW_S = CONVERSATION_WINDOW_S  # backwards-compatible alias
PUSH_TO_TALK_ENABLED = _env_bool("EV_PUSH_TO_TALK_ENABLED", True)
PUSH_TO_TALK_KEY = _env("EV_PUSH_TO_TALK_KEY", "<ctrl>+<alt>+e")


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
- Your reply is fed straight to a speech synthesiser and read aloud \
exactly as written. Never prefix it with a label of any kind: no "Spoke:", \
no "E.V.:", no "Response:", "Reply:", "Answer:" or "Assistant:". No quote \
marks wrapped around the whole reply, no JSON, no stage directions. Just \
the words you want said.

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
- terminal_command is the last resort. Never use it to launch a GUI app, \
open a web page, or touch a file - that is what open_app, web_search and \
file_manager are for.
- Anything involving a file or folder goes through file_manager: creating, \
reading, listing, copying, moving, renaming, deleting, searching, tidying. \
When asked to create a file with content, write the real content out in \
full in the content argument - never promise to do it later, and never \
hand back a placeholder.
- Never invent a file path. If the user named no directory, omit the argument \
and let the default apply.
- Chit-chat, questions, opinions, and anything needing no machine action go \
through chat.

You always answer with a tool call. chat is the fallback."""
