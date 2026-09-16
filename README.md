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
| LLM — `llama-3.3-70b-versatile` | Groq Cloud | one HTTPS request |
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
                                     │
                          ┌──────────┼──────────┬──────────────┐
                          ▼          ▼          ▼              ▼
                      open_app   web_search  dev_workflow  terminal_command
                          │          │          │              │
                          └──────────┴─────┬────┴──────────────┘
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
requirements.txt      six packages
quickstart.md         setup, sample commands, tuning, troubleshooting

ev/
  brain.py            Groq + Gemini over raw HTTP, returns a tool call
  stt.py              speech to text: Groq Whisper, Google, or whisper.cpp
  tts.py              edge-tts synthesis and playback
  audio.py            microphone capture with VAD, MP3 playback via winmm
  wake.py             fuzzy wake-phrase matching
  session.py          conversation state and local control phrases
  tts_voices.py       list and audition Edge voices

tools/
  __init__.py         registry and dispatch, with argument filtering
  schemas.py          JSON tool definitions, translated per provider
  safety.py           command risk classification
  base.py             process launching, executable and path resolution
  app_launcher.py     open_app
  browser.py          web_search
  dev_workflow.py     VS Code + integrated terminal + Claude Code
  terminal.py         terminal_command
  window.py           Win32 focus helpers, so keystrokes never go astray

tests/
  test_smoke.py       tools, safety, wake phrase — offline
  test_brain.py       provider wire formats, event loop — mocked
```

## Tools

| Tool | Does |
|---|---|
| `open_app` | Launches desktop apps. Fuzzy-matches misheard names, resolves through PATH and the Windows App Paths registry. |
| `web_search` | Opens a search or a URL, in a named browser or the default. |
| `dev_workflow` | Opens VS Code on a project, spawns an integrated terminal, starts Claude Code, optionally types an opening prompt. |
| `terminal_command` | Runs shell commands, gated by the safety classifier. Foreground with output captured, or detached into its own window. |
| `chat` | Speaks a reply when no action is called for. |

Control phrases ("take five", "wake up", "stop", "goodbye") are matched in
[ev/session.py](ev/session.py) before the model is consulted, so they respond
instantly and work even while E.V. is talking.

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
python tests/test_smoke.py    # 20 tests
python tests/test_brain.py    # 10 tests
```

Both run offline with no API key and open no windows.

## Requirements

Python 3.10+, a microphone, and a free API key from
[Groq](https://console.groq.com/keys) or
[Google AI Studio](https://aistudio.google.com/apikey).

Windows is the primary target. The core loop, brain, STT and TTS are portable;
`dev_workflow`'s integrated-terminal path and the `winmm` player are
Windows-specific, and both degrade to documented fallbacks elsewhere.
