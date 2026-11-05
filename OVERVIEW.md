# L.E.O. Project Overview

L.E.O. (Latency-optimized Executive Operator, spoken "Leo") is a voice assistant for the desktop. You talk to it, and it opens programs, manages files, runs shell commands, drives a web browser, and can operate the mouse and keyboard to finish a task on screen. It runs on Windows and Ubuntu.

The main design constraint is memory. Leo runs no local model, so it uses roughly 90 to 160 MB of RAM. Language understanding, speech recognition and speech synthesis are all network calls. What runs locally is a Python event loop, a 16 kHz microphone stream, and the code that performs actions.

This document covers what Leo can do, how it is structured, and the implementation choices behind it. Setup instructions are in [quickstart.md](quickstart.md).

## Capabilities

**Applications and files**
- Launches installed programs by name, including ones that are not on `PATH`. On Windows it searches the App Paths registry and the Start Menu. On Linux it reads `.desktop` entries.
- Creates, reads, copies, moves, renames, deletes, searches and organises files. It can also work on a whole folder at once, for example "move every PDF in Downloads to Documents".
- Opens folders in the file manager. It checks that the folder exists first, so a wrong path is reported instead of silently opening somewhere else.

**Shell and development**
- Runs terminal commands and reads the output back.
- Opens a project in VS Code, starts a terminal in it, and can launch Claude Code with an opening prompt.
- Runs supervised coding sessions: `claude -p` in its own git worktree and branch, with a spending limit, then runs the tests itself and reports the result aloud. It never pushes or merges.
- Reports CPU, memory, disk, processes and listening ports, and starts, stops and reads the logs of servers it launched.

**People and messages**
- Files notes on a Memory Tree by person and topic, with links between them, and brings the right branch into the conversation when a name comes up.
- Reads and searches WhatsApp through [`wacli`](https://github.com/openclaw/wacli), fetches files, sends files to your phone over KDE Connect, and drafts messages in your style. Every send reads the exact text aloud and waits for your yes. wacli is unofficial, so WhatsApp's terms and a possible ban are real risks; see [quickstart.md](quickstart.md).

**Web**
- Opens searches, sites and common destinations ("open my email").
- Completes multi-step web tasks in a real browser through Playwright: searching, filtering, filling forms, adding items to a basket, and reading results back. It works from the page structure rather than screenshots.

**Screen**
- Takes a screenshot and answers questions about it ("what does that error say").
- Clicks, types and uses keyboard shortcuts in desktop applications, checking the screen again after each step.
- Runs longer errands on its own (`agent_task`) with an on-screen overlay and a kill switch.

**Everyday**
- Media playback and volume control.
- Remembers facts about the user and keeps a to-do list across sessions.
- Keeps a backlog of anything that was interrupted or left unfinished, and reports it on the next start.

**Presence**
- An optional animated face that shows what Leo is doing (listening, thinking, speaking, and so on) and displays captions of the conversation.

## Architecture

```
microphone -> voice activity detection -> Whisper (Groq) -> transcript
                                                              |
                                          wake phrase and local control phrases
                                                              |
                                          language model (Groq or Gemini)
                                                              |
                                             tool call with JSON arguments
                                                              |
                                      safety checks -> tool -> result
                                                              |
                                          spoken reply (edge-tts) and face
```

A single asyncio loop in [leo_core.py](leo_core.py) owns the whole cycle: listen, transcribe, check for local intents, ask the model, act, speak. Anything that blocks (microphone reads, subprocesses, keystrokes, tool calls) runs through `asyncio.to_thread`, so a slow tool never freezes the loop or blocks Ctrl+C.

### Main components

| Area | Files | Role |
|---|---|---|
| Core loop | `leo_core.py` | Conversation flow, confirmations, cancellation, follow-up questions |
| Model | `leo/brain.py` | Groq and Gemini over plain HTTP, rate-limit handling, provider failover |
| Hearing | `leo/audio.py`, `leo/stt.py`, `leo/wake.py`, `leo/voice.py` | Microphone capture, transcription, wake phrase, voice and echo detection |
| Speaking | `leo/tts.py` | Speech synthesis, playback, and cleanup of text before it is spoken |
| Session | `leo/session.py`, `leo/memory.py`, `leo/backlog.py` | Conversation state, stored facts, unfinished work |
| Tools | `tools/` | One module per capability, plus the shared registry and safety checks |
| Face | `leo/face/` | The animated face, run as a separate process |

### Tools

The model never produces text that gets executed directly. It picks a tool and fills in its arguments according to a JSON schema. [tools/schemas.py](tools/schemas.py) defines every tool once and converts the definitions to each provider's format. [tools/\_\_init\_\_.py](tools/__init__.py) is the single entry point: it drops any argument the schema does not declare, coerces loosely typed values, and turns exceptions into failure results so a broken tool cannot crash the assistant.

The current tools are `open_app`, `web_search`, `file_manager`, `terminal_command`, `dev_workflow`, `take_screenshot`, `mouse_action`, `keyboard_action`, `screen_task`, `browser_task`, `agent_task`, `media_control`, `remember_fact`, `recall_fact`, `manage_todo`, `backlog` and `chat`.

To keep requests small, only the tools relevant to an utterance are sent to the model each turn. If the model cannot answer with that subset, the full set is sent on the retry.

## Implementation notes

### Staying small

- No vendor SDKs. Each provider is a hand-built JSON request through `httpx`, using one shared connection pool.
- No audio libraries. On Windows, MP3 playback goes through `winmm` via `ctypes`. On Ubuntu it uses GStreamer, which ships with the OS.
- No wake-word model. Every utterance is transcribed anyway, so the wake phrase is matched in the transcript.
- Heavier pieces are optional and loaded only when needed: screenshot support (`mss`, Pillow), Playwright for the browser, and PySide6 for the face.

### Safety

Voice input is unreliable, so Leo assumes the model can be wrong.

- Shell commands are classified as safe, needing confirmation, or blocked. Blocked commands, such as formatting a disk or piping a download into a shell, never run.
- File operations are restricted to configured root folders. A path outside them is refused, never redirected.
- Clicks and typed text are checked against a second classifier that reads what the action is about to do, such as the label on a button. Anything that spends money, sends a message, deletes something or touches a password needs a spoken "yes".
- Every confirmation asks the same question, "Confirm?", and only a clear yes counts.
- Saying "lockdown" or "stop everything", or pressing Ctrl+Alt+Q during an autonomous run, stops all actions until the user unlocks Leo by voice. The model has no way to unlock it.
- A rate limiter locks Leo down if it starts repeating the same action in a loop.
- Every action is written to an audit log.
- Text read from web pages and files is marked as untrusted when it is passed back to the model, so instructions inside a page are not treated as instructions from the user.

### Web and screen automation

Web tasks avoid screenshots where possible. Leo reads the page, numbers every clickable element, and the planner responds with steps like `click 12` or `fill 3 = gaming mouse`. This costs far fewer tokens than a vision request and is more precise than guessing CSS selectors. It also tracks irreversible clicks, such as "Add to basket", so the same item is not added twice.

Desktop tasks use screenshots. Coordinates are expressed as fractions of the screen, a labelled grid is drawn on the image to help the model read positions accurately, and the screen is checked again after every step. If the screen stops changing, the task ends instead of clicking indefinitely.

### Conversation

- After the wake phrase, the conversation stays open for a few seconds so follow-ups do not need the name.
- Control phrases such as "stop", "take five" and "wake up" are matched locally and take effect immediately.
- The user can interrupt Leo while it is speaking. To avoid Leo hearing itself on speakers, it compares the input against its own playback level, learns a simple voiceprint for both the user and its own voice, and discards transcripts that repeat what it just said.
- Whisper's confidence scores are used to reject or flag unclear transcripts rather than act on them.
- Requests with two parts, such as "open my mail and summarise it", are handled as two turns.

### The face

The face is a PySide6 window running in its own process. The core sends it JSON lines over stdin describing the current state, so the main process never loads Qt. Moods are defined in `leo/face/expressions.json` as eye and lid parameters, and the renderer blends smoothly between them. The face appears when Leo is addressed, hides after the conversation ends, and stays hidden while a full-screen application is in use. On Ubuntu it runs through XWayland, because Wayland does not allow windows to keep themselves on top.

### Persistence

Memory, the to-do list and the backlog are small JSON files written atomically. A corrupted file is set aside with a warning, and Leo starts with empty state instead of failing to boot.

## Testing

The suite has about 870 tests and runs offline, with no API key, no windows and no access to real user folders.

```bash
python -m pytest tests/ -q
```

A separate set of live browser tests runs against real websites when `LEO_LIVE_BROWSER=1` is set.

## Configuration

All settings are in [config.py](config.py) and can be overridden with `EV_*` environment variables or a `.env` file. [.env.example](.env.example) documents each one. The only required setting is a Groq or Gemini API key.
