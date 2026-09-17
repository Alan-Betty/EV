# E.V. — Everyday Virtual Assistant

A fast, lightweight desktop voice assistant that controls your machine.
Free cloud-hybrid architecture: the intelligence lives in the cloud, the hands
are local Python.

Built to run comfortably on a 4 GB machine.

```
you  > hey EV open Chrome and look for a good gaming mouse
E.V. > On it.
you .> launch VS Code and start Claude Code on my API project
E.V. > Claude's running on my-api with your prompt.
you .> take five
E.V. > Standing by.
```

Say the name once. After that you just talk - the `.` means the conversation
is still open. "Take five" puts E.V. to sleep instantly, mid-sentence if need
be; "wake up" brings it back.

**[Setup instructions and sample commands → quickstart.md](quickstart.md)**

---

## Why it is small

Local voice assistants are heavy because they run a language model, a speech
recogniser and a speech synthesiser on your hardware. Each one costs gigabytes.

E.V. runs none of them locally:

| Component | Where | Local footprint |
|---|---|---|
| LLM — best model on your Groq account | Groq Cloud | one HTTPS request |
| Speech recognition — Whisper | Groq Cloud | one HTTPS upload |
| Speech synthesis — Edge Neural | Microsoft | one WebSocket, a temp MP3 |
| Audio playback | Windows `winmm`, via `ctypes` | none |
| Wake phrase | string match on the transcript | none |
| Control phrases | matched locally, no network | none |

What is left locally is a Python event loop, a 16 kHz mono audio stream, and
`subprocess` calls. Resident memory lands around **90–160 MB**.

The design goes further than just picking cloud services:

- **No vendor SDKs.** Groq and Gemini are each one JSON POST, so E.V. calls them
  with `httpx` directly. That drops roughly 40 MB and a large dependency tree.
- **No audio library.** MP3 playback goes through the Windows MP3 decoder over
  `ctypes`. No `pygame`, no `ffmpeg`, no `pydub`.
- **No wake-word model.** The utterance is being transcribed anyway, so the wake
  phrase is a string match — not a keyword-spotting network held in memory.
- **One pooled HTTP client** shared by the brain and the transcriber, so no
  utterance pays for a TLS handshake.

## Architecture

```
  microphone ──► VAD ──► Groq Whisper ──► transcript
                                              │
                                    wake phrase match
                                              │
                                   local control phrase? ──► standby / resume
                                              │                  (no network)
                                              ▼
                              Groq / Gemini + JSON tool schema
                                              │
                                     ┌────────┴────────┐
                                     ▼                 ▼
                              safety classifier     chat
                                     │              backlog
                                     │              remember
                                     │
                ┌──────────┬──────────┼────────────┬────────────┐
                ▼          ▼          ▼            ▼            ▼
            open_app  web_search  file_manager  dev_workflow  terminal_command
                │          │           │            │            │
                └──────────┴─────┬─────┴────────────┴────────────┘
                                 ▼
                             edge-tts ──► winmm ──► speakers
```

The model never emits free text that gets executed. It selects a tool and fills
in a JSON schema, `tools/schemas.py` defines those once and translates them for
each provider, and `tools/__init__.py` filters the arguments down to what the
schema actually declares before anything runs.

## Files

```
ev_core.py            async event loop: capture, transcribe, decide, act, speak
config.py             all configuration, environment-overridable
.env.example          annotated template — copy to .env
requirements.txt      seven packages
quickstart.md         setup, sample commands, tuning, troubleshooting

ev/
  brain.py            Groq + Gemini over raw HTTP, returns a tool call
  stt.py              speech to text: Groq Whisper, Google, or whisper.cpp
  tts.py              edge-tts synthesis, playback, and the no-labels boundary
  ui.py               rich terminal UI — renders only, returns nothing speakable
  audio.py            microphone capture with VAD, MP3 playback via winmm
  wake.py             fuzzy wake-phrase matching
  session.py          conversation state and local control phrases
  memory.py           preferences and runtime state, atomic JSON on disk
  backlog.py          what the last session never finished
  tts_voices.py       list and audition Edge voices

tools/
  __init__.py         registry and dispatch, with argument filtering
  schemas.py          JSON tool definitions, translated per provider
  safety.py           command risk classification
  base.py             process launching, executable and path resolution
  app_launcher.py     open_app
  browser.py          web_search
  file_manager.py     file_manager — root-scoped file and folder control
  dev_tools.py        VS Code + integrated terminal + Claude Code
  terminal.py         terminal_command
  backlog.py          backlog — read back, tick off, retry
  memory.py           remember — store and recall small facts
  window.py           Win32 focus helpers, so keystrokes never go astray

tests/
  test_smoke.py              tools, safety, wake phrase — offline
  test_brain.py              provider wire formats, event loop — mocked
  test_speech_purity.py      no label ever reaches the speaker
  test_file_manager.py       file operations and root containment
  test_file_batch.py         whole-folder operations, still contained
  test_memory.py             state that survives a reboot or a power cut
  test_backlog.py            unfinished work, and never pre-approving it
  test_acknowledgement.py    "stand by" runs alongside the work, not before
  test_streaming_and_ui.py   partial-JSON parsing, UI/speech separation
  test_standby_and_stt.py    waking up, app lookup, recognition confidence
  test_cancel.py             stopping work already underway, safely
  test_stream_integration.py SSE frames in, spoken sentences out
```

## Tools

| Tool | Does |
|---|---|
| `open_app` | Launches desktop apps. Fuzzy-matches misheard names, and resolves through PATH, the Windows App Paths registry, and an index of every Start Menu shortcut — so it finds programs that were never added to PATH and were never listed in config. |
| `web_search` | Opens a search or a URL, in a named browser or the default. |
| `file_manager` | Creates, reads, lists, copies, moves, renames, deletes, searches and organises files, singly or by the folderful. Scoped to `EV_FILE_ROOTS`; deletes, reorganises, batch moves and batch renames always ask first, and deletes go to the Recycle Bin. |
| `dev_workflow` | Opens VS Code on a project, spawns an integrated terminal, starts Claude Code, optionally types an opening prompt. |
| `terminal_command` | Runs shell commands, gated by the safety classifier. Foreground with output captured, or detached into its own window. |
| `backlog` | Reads back, ticks off, or retries what the last session left unfinished. |
| `remember` | Keeps a small fact about you between sessions, or looks one up. |
| `chat` | Speaks a reply when no action is called for. |

Control phrases ("take five", "wake up", "stop", "goodbye") are matched in
[ev/session.py](ev/session.py) before the model is consulted, so they respond
instantly and work even while E.V. is talking. In standby, waking up is matched
loosely — "hey, wake up" and "you awake?" both work — and anything that does
not match draws an on-screen reminder rather than nothing at all.

Saying "stop" while something is already running now actually stops it. A
shell command is killed mid-run; a batch file operation finishes the file it
is on and leaves the rest alone, then tells you exactly how many went. What it
will not do is pretend: a program that has already launched cannot be
un-launched, and E.V. says so rather than claiming otherwise. Anything you say
over a running tool that *wasn't* "stop" is kept and treated as your next
command.

Whisper reports how confident it was in every transcript, and E.V. acts on
that: a bad one becomes *"Didn't catch that"* instead of a confidently wrong
action, and a borderline one is passed to the model with a note that the words
may be off. The decoding prompt is built from your own machine — installed
programs, your folders, what you just said — which is the cheapest accuracy
fix there is.

Anything slower than a moment gets an immediate "on it, stand by" — spoken
from a background task while the work is already running, so it costs the
action nothing. Anything that does not finish ends up on the backlog and is
read back on the next boot: *"we have two backlog items remaining from your
previous session."* Retrying one puts it through the same confirmation it
faced the first time.

## Safety

Voice input is unreliable, so nothing destructive runs on the model's say-so.

**Blocked outright** — disk formatting, `rm -rf /`, shadow-copy deletion,
pipe-to-shell from the internet, fork bombs, boot configuration. No confirmation
unlocks these.

**Held for confirmation** — deleting, moving, killing processes, installing
software, registry edits, shutdown, irreversible git operations. E.V. says what
the command would do and waits. Anything that is not an unambiguous yes is
treated as no.

Keystroke automation is gated the same way: `dev_workflow` only types into
VS Code's integrated terminal once the Win32 layer confirms that window holds
focus. If it cannot, it falls back to spawning a separate terminal rather than
typing into whatever happened to be on screen.

## Tests

```bash
python -m pytest tests/ -q    # 130 tests
```

All of them run offline with no API key, open no windows, and touch no real
user directory — the file tests redirect `FILE_ROOTS` at a temporary tree.

Two suites are load-bearing rather than incidental:

* `test_speech_purity.py` is the regression suite for E.V. reading labels
  aloud. It covers both the structural cause (a tool observation stored as an
  assistant turn) and the boundary that strips labels regardless of origin.
  Its keep-cases matter as much as its strip-cases: a stripper aggressive
  enough to eat "Spoke to your mother" would be its own bug.
* `test_file_manager.py` and `test_file_batch.py` pin the containment
  guarantee. A path that escapes `FILE_ROOTS` must be refused, never silently
  retargeted — and a batch action checks every file it touches, not just the
  folder it was pointed at.
* `test_backlog.py` pins the rule that keeps a saved command from becoming a
  standing permission: a replayed item drops its confirmation, so a delete
  declined on Monday is asked about again on Tuesday.

## Requirements

Python 3.10+, a microphone, and a free API key from
[Groq](https://console.groq.com/keys) or
[Google AI Studio](https://aistudio.google.com/apikey).

`rich` gives the terminal UI its panels and spinners; without it everything
still works and simply renders as plain text. `send2trash` is strongly
recommended, because it is what makes a confirmed delete recoverable.

Windows is the primary target. The core loop, brain, STT and TTS are portable;
`dev_workflow`'s integrated-terminal path and the `winmm` player are
Windows-specific, and both degrade to documented fallbacks elsewhere.
