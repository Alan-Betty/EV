<div align="center">

# E.V.

### Everyday Virtual Assistant

**A voice assistant that actually runs your Windows desktop — in about 120 MB of RAM.**

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)](#platform)
[![Tests](https://img.shields.io/badge/tests-321%20passing-success)](#testing)
[![Memory](https://img.shields.io/badge/RAM-90--160%20MB-blueviolet)](#why-its-small)
[![Dependencies](https://img.shields.io/badge/dependencies-8-lightgrey)](#requirements)

*No local model. No vendor SDKs. No audio library. Just an event loop and some very deliberate choices.*

</div>

---

```
you  > hey EV, open Chrome and look for a good gaming mouse
E.V. > On it.
you .> launch VS Code and start Claude Code on my API project
E.V. > Claude's running on my-api with your prompt.
you .> tidy up my downloads folder
E.V. > On it, stand by.
E.V. > Forty-one files, seven folders. Downloads is civilised again.
you .> take five
E.V. > Standing by.
```

Say the name once. After that you just talk — the `.` means the conversation is
still open. It closes after **8 seconds** of quiet, and then the name is needed
again. *"Take five"* puts E.V. to sleep instantly, mid-sentence if need be;
*"wake up"* brings it back.

---

## Contents

- [Quick start](#quick-start)
- [What you can say](#what-you-can-say)
- [Why it's small](#why-its-small)
- [Architecture](#architecture)
- [Tools](#tools)
- [Safety](#safety)
- [Talking to it](#talking-to-it)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Requirements](#requirements)
- [Platform](#platform)

---

## Quick start

```bash
git clone https://github.com/<you>/EV.git
cd EV
python -m pip install -r requirements.txt

cp .env.example .env          # then put your API key in it
python ev_core.py --check     # verify the setup before you talk to it
python ev_core.py             # voice loop
```

You need one free API key, from
[Groq](https://console.groq.com/keys) (default) or
[Google AI Studio](https://aistudio.google.com/apikey).

### Ways to run it

| Command | What it does |
|---|---|
| `python ev_core.py` | Voice loop. The normal way. |
| `python ev_core.py --text` | Typed input, same brain and same tools. Good for testing without a microphone. |
| `python ev_core.py --say "open notepad"` | Run one command, then exit. |
| `python ev_core.py --check` | Configuration readiness report. Exits `1` if anything is wrong. |
| `python ev_core.py -v` | Debug logging. |
| `python -m ev.tts_voices` | List the available Edge voices. |
| `python -m ev.tts_voices --demo en-US-AriaNeural` | Audition one. |

> [!TIP]
> `--check` is the fastest way to diagnose a broken environment. It verifies the
> API key, confirms the model exists on *your* account, names an input device,
> prints the resolved file roots, and confirms the state directory is writable —
> a read-only one means a silently forgetful assistant.

**[Full setup, tuning and troubleshooting → quickstart.md](quickstart.md)**

---

## What you can say

| You say | E.V. does |
|---|---|
| *"open Spotify"* | Launches it — even if it was never on `PATH`. |
| *"open File Explorer at my GitHub folder"* | Opens the real folder. Says so honestly if it is not there. |
| *"open my email"* | Goes to your inbox instead of asking which provider. |
| *"open my mail and summarise the important things"* | Two jobs. Opens it, looks at it, tells you. |
| *"search for mechanical keyboards in Firefox"* | One action, not two. |
| *"make a file called notes.txt on my desktop with my shopping list"* | Writes the real content, not a placeholder. |
| *"organise my downloads folder"* | Sorts by type into folders. Asks first. |
| *"copy all the PDFs from downloads to documents"* | Batch operation, every path re-checked. |
| *"what's my Python version"* | Runs it, reads back the answer. |
| *"open VS Code on my API project and start Claude"* | Opens, focuses, types the prompt. |
| *"remember I prefer Firefox"* | Persists between sessions. |
| *"what's still on the backlog"* | Reads back what the last session never finished. |
| *"what's on my screen"* | Looks, and tells you. No local vision model. |
| *"what does that error say"* | Reads the dialog back to you. |
| *"turn my display scaling up to 125 percent"* | Drives the settings app itself, looking between each step. |
| *"find a wireless mouse on Amazon and add the top one to my cart"* | Real browser, real DOM, not pixel-guessing. |
| *"buy it"* | Asks first. Always. |
| *"stop"* | Stops the thing that is currently running. Actually stops it. |

---

## Why it's small

Local voice assistants are heavy because they run a language model, a speech
recogniser and a speech synthesiser on your hardware. Each one costs gigabytes.

E.V. runs **none** of them locally:

| Component | Where it runs | Local footprint |
|---|---|---|
| **LLM** — best model on your Groq account | Groq Cloud | one HTTPS request |
| **Speech recognition** — Whisper large-v3 | Groq Cloud | one HTTPS upload |
| **Speech synthesis** — Edge Neural voices | Microsoft | one WebSocket, a temp MP3 |
| **Audio playback** | Windows `winmm`, via `ctypes` | none |
| **Wake phrase** | string match on the transcript | none |
| **Control phrases** | matched locally, no network at all | none |

What is left is a Python event loop, a 16 kHz mono audio stream, and `subprocess`
calls. Resident memory lands around **90–160 MB**.

The design goes further than just picking cloud services:

- **No vendor SDKs.** Groq and Gemini are each a single hand-built JSON POST, so
  E.V. calls them with `httpx` directly. That drops roughly 40 MB and a large
  dependency tree.
- **No audio library.** MP3 playback goes through the Windows MP3 decoder over
  `ctypes`. No `pygame`, no `ffmpeg`, no `pydub`.
- **No wake-word model.** The utterance is being transcribed anyway, so the wake
  phrase is a string match — not a keyword-spotting network held in memory.
- **One pooled HTTP client** shared by the brain and the transcriber, so no
  utterance pays for a TLS handshake.
- **Nothing blocks the loop.** Every blocking call — microphone reads,
  `subprocess`, keystrokes, tool dispatch — goes through `asyncio.to_thread`, so
  a slow tool never stalls the loop or swallows <kbd>Ctrl</kbd>+<kbd>C</kbd>.

---

## Architecture

```
  microphone ──► VAD ──► Groq Whisper ──► transcript + confidence
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

One async loop in [`ev_core.py`](ev_core.py) owns all of it:
`listen → transcribe → local intent? → decide → act → speak`.

The model never emits free text that gets executed. It selects a tool and fills
in a JSON schema. [`tools/schemas.py`](tools/schemas.py) defines those schemas
exactly once and translates them per provider, and
[`tools/__init__.py`](tools/__init__.py) filters the arguments down to what the
schema actually declares before anything runs.

### Streaming

With streaming on, a conversational reply starts playing while the model is
still writing it — the `reply` string is pulled out of half-finished tool-call
JSON and handed to the speaker sentence by sentence. **Only `chat` streams.**
Every other tool has a side effect, and announcing *"Chrome's up"* before Chrome
is up would be a lie.

### Immediate acknowledgement

Anything slower than a moment gets an *"on it, stand by"* — spoken from a
background task while the work is **already running**, so it costs the action
nothing. It is also delayed slightly: a tool that returns in 200 ms needs no
acknowledgement, and the task is cancelled before it ever speaks.

---

## Tools

| Tool | What it does |
|---|---|
| `open_app` | Launches desktop apps. Fuzzy-matches misheard names and resolves through `PATH`, the Windows App Paths registry, and an index of every Start Menu shortcut — so it finds programs that were never added to `PATH` and were never listed in config. |
| `web_search` | Opens a search or a URL, in a named browser or the default. |
| `file_manager` | Creates, reads, lists, **opens**, copies, moves, renames, deletes, searches and organises files — singly or by the folderful. Scoped to `EV_FILE_ROOTS`. |
| `dev_workflow` | Opens VS Code on a project, spawns an integrated terminal, starts Claude Code, optionally types an opening prompt. |
| `terminal_command` | Runs shell commands, gated by the safety classifier. Foreground with output captured, or detached into its own window. |
| `take_screenshot` | Looks at the screen and answers a question about it. Pass a `region` to crop before the downscale and read small text at full resolution. The frame is grabbed to memory — nothing is written to disk unless you ask. |
| `mouse_action` | Moves, clicks, right-clicks, drags and scrolls the real pointer. Coordinates are fractions of the screen, so they survive the downscale. |
| `keyboard_action` | Types text or sends hotkeys to whatever has focus. Typed text is checked against the same blocked patterns as `terminal_command`. |
| `screen_task` | The autonomous loop: capture, decide, act, look again. It opens apps, waits for windows, focuses them, clicks, types and sends shortcuts, so "open Notepad and type hello" is one call rather than a launch that drops the typing. Bounded by a step count and a timeout. |
| `browser_task` | Drives a real browser through the DOM — navigate, fill, filter, click by visible text, read results back. Keeps a profile between tasks, so it starts signed in to whatever you signed it in to. Better than pixels for anything on the web. |
| `agent_task` | The loop above those two. Takes over, draws an overlay saying so, and keeps choosing what to do next until the errand is actually finished: *"find me a gaming mouse under 5000 and put it in my basket"*. On a website it works through the DOM and spends **no vision tokens at all**; the screen is for desktop work and for when the browser route gets stuck. Bounded by rounds, a wall clock and a stall detector, and stoppable at any moment. |
| `backlog` | Reads back, ticks off, or retries what the last session left unfinished. |
| `remember` | Keeps a small fact about you between sessions, or looks one up. |
| `chat` | Speaks a reply when no action is called for. |

> Adding a tool is three things and nothing else: a spec in `TOOL_SPECS`, an
> implementation returning a `ToolResult`, and an entry in `REGISTRY`. Nothing
> outside the schema can reach the function.

---

## Safety

Voice input is unreliable, so nothing destructive runs on the model's say-so.
Two independent gates enforce that.

<table>
<tr>
<th width="50%">🚫 Blocked outright</th>
<th width="50%">⚠️ Held for confirmation</th>
</tr>
<tr>
<td>

Disk formatting, `rm -rf /`, shadow-copy deletion, pipe-to-shell from the
internet, fork bombs, boot configuration.

**No confirmation unlocks these.**

</td>
<td>

Deleting, moving, killing processes, installing software, registry edits,
shutdown, irreversible `git` operations.

E.V. says what the command would do and waits. Anything that is not an
unambiguous *yes* is treated as *no*.

</td>
</tr>
</table>

**Files are fenced.** Every path resolves through `realpath` and is refused if it
falls outside `EV_FILE_ROOTS` — never silently retargeted. That includes paths
handed to `open_app`: a model that invents `C:\Users\Alan\GitHub` gets a
refusal, not a launch, because Explorer opens its default location for a path
that is not there and exits 0 — so "it worked" and "it went nowhere" look
identical from the outside. Batch operations
re-check *every* source and *every* destination, not just the two folders named
in the call, because a leaked root would leak by the hundred. Deletes go to the
Recycle Bin when `send2trash` is installed, so a mistaken *yes* is recoverable.

**Keystrokes are fenced too.** `dev_workflow` only types into VS Code's
integrated terminal once the Win32 layer confirms that window holds focus. If it
cannot confirm, it spawns a separate terminal rather than typing into whatever
happened to be on screen.

**Clicks are classified before they land.** Driving the real mouse has no
sandbox and no undo, so a second classifier reads the *description* of what is
about to happen — the button label, the text about to be typed, the goal of an
autonomous run — because no pattern over a command line will ever notice that
the button under the pointer says *Place order*. Anything that spends money,
sends something other people will see, destroys something, or touches a
credential is held for a spoken yes. Text typed at the keyboard also goes
through the shell classifier, so `format c:` is refused whether it arrives
through `terminal_command` or through a focused terminal window.

`browser_task` checks every step before the browser even opens, so a purchase
buried at step five is asked about at step zero. `screen_task` cannot do that —
it only discovers its next move by looking — so it stops and asks when it gets
there. Saying yes re-runs the same goal, which is safe because the loop re-reads
the screen rather than replaying what it already did. Where it does plan a few
actions at once, all of them are classified before any of them runs.

**A backlog entry is a reminder, never a signed permission slip.** A replayed
item drops its confirmation, so a delete declined on Monday is asked about again
on Tuesday.

### How a mission actually runs

A web errand never looks at the screen. The page is already text, so E.V.
reads it: the URL, the title, every element you could click or type into -
each one numbered - and the visible words. The planner answers `click 12` or
`fill 3 = gaming mouse`, and the number is attached to the real element, so
it cannot miss the way a guessed CSS selector can.

That is worth roughly twenty rounds where looking at the screen was worth
four, because a screenshot costs about 1,900 tokens of a per-minute budget of
8,000 and a page read costs a fraction of that on a separate budget. Measured
end to end on real sites: the cheapest book in a category in 2 rounds, the
cheapest gaming mouse on Amazon in 3, a named phone added to a shop basket
and verified in 6.

The screen loop is still there for desktop work, and a mission falls back to
it by itself when the browser route reports that it is the wrong tool.

```bash
EV_LIVE_BROWSER=1 python -m pytest tests/test_web_agent_live.py -q
```

runs the browser layer against real sites (example.com, books.toscrape.com,
DuckDuckGo, Wikipedia, and a full log-in-and-add-to-cart flow on
saucedemo.com). The rest of the suite is offline and stays offline.

### Letting it run on its own

`agent_task` is the only thing here that works unattended for minutes at a
time, so it asks once, before anything moves — *"I'll take over the screen and
run this until it's done. Confirm?"* — and what that yes covers is the errand
you described. Every sub-goal is classified again on the way past, and one
carrying a **new** kind of risk stops the run and asks: a basket errand does
not authorise the checkout that turns up at round nine. Saying yes there
resumes from where it got to rather than starting again, because the loop
re-reads the screen instead of replaying itself.

While it runs, a red frame is drawn round the screen and a badge names the
errand and the round. Both are click-through, so neither the frame nor the
badge can ever intercept a click meant for the page underneath, and neither
takes focus away from whatever is being typed into.

**The kill switch works from anywhere**, because you will be watching the
thing E.V. is driving, not E.V.:

| Route | What it does |
|---|---|
| <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Q</kbd> | Registered with the window manager, so it lands even from a full-screen app. `EV_AGENT_KILL_HOTKEY` changes it. |
| *"stop everything"* | Matched locally, acted on the instant it is heard — not after the running tool finishes. |
| <kbd>Ctrl</kbd>+<kbd>C</kbd> | The usual. |

All three cancel the run where it stands and lock E.V. down, so nothing else
runs until you say *"unlock"*. Someone reaching for a kill switch means
everything, not this click.

---

## Talking to it

### Conversation, standby, and the wake phrase

E.V. is in one of three states:

| State | Behaviour |
|---|---|
| **Idle** | The wake phrase is required. |
| **Engaged** | Mid-conversation — just talk. Decays back to idle after 8 seconds of quiet. |
| **Standby** | Ignores everything but *"wake up"*. |

Control phrases — *"take five"*, *"wake up"*, *"stop"*, *"goodbye"* — are matched
in [`ev/session.py`](ev/session.py) **before** the model is consulted, so they
land instantly and work even while E.V. is talking. Matching there is exact on
purpose: a fuzzy match would mistake *"stop the server"* for a cancel and drop a
real request.

Waking up is the one deliberate exception — *"hey, wake up"* and *"you awake?"*
both work, because standby has exactly two exits and no commands to swallow.
Anything that doesn't match draws an on-screen reminder rather than nothing at
all.

### Stopping something already underway

Saying *"stop"* while a tool is running actually stops it. A shell command is
killed mid-run; a batch file operation finishes the file it is on, leaves the
rest alone, and tells you exactly how many went.

What it will **not** do is pretend. A program that has already launched cannot
be un-launched, and E.V. says so rather than claiming otherwise. Anything you
say over a running tool that *wasn't* "stop" is kept and handled as your next
command, because talking over a slow tool is usually the next thing you wanted.

### It knows when it misheard you

Whisper reports how confident it was in every transcript, and E.V. acts on that.
A bad transcript becomes *"Didn't catch that"* instead of a confidently wrong
action; a borderline one goes to the model with a note that the words may be off,
so it can ask instead of guessing.

The decoding prompt is built per-utterance from your own machine — installed
programs, your folder names, open backlog items, and what you just said. That is
the cheapest accuracy fix available, and it is why *"E.V."* stops coming back as
*"Eevee"*.

### It remembers

Preferences and unfinished work survive a reboot, a lid, or a power cut. Both
are small JSON files written atomically, and a corrupt one is a warning rather
than an error — losing preferences is survivable, refusing to boot is not.

On the next start you get the gap, your stored facts, and what's still open:
*"we have two backlog items remaining from your previous session."*

---

## Configuration

Everything lives in [`config.py`](config.py), and every value is overridable by
an `EV_*` environment variable or a line in `.env`.
[`.env.example`](.env.example) is the annotated template.

The settings worth knowing about:

| Variable | Default | What it controls |
|---|---|---|
| `GROQ_API_KEY` | — | Your key. The one required setting. |
| `EV_LLM_PROVIDER` | `groq` | `groq` or `gemini`. |
| `EV_CONVERSATION_WINDOW_S` | `8` | How long you can keep talking without the wake phrase. |
| `EV_WAKE_PHRASES` | `ev, hey ev, …` | What wakes it. |
| `EV_TTS_VOICE` | `en-US-GuyNeural` | Which voice. `python -m ev.tts_voices` lists them. |
| `EV_TTS_RATE` | `+18%` | How fast it talks. |
| `EV_FILE_ROOTS` | your home folder | The only place `file_manager` may touch. |
| `EV_ALLOW_SHELL` | `true` | Whether `terminal_command` exists at all. |
| `EV_STATE_DIR` | `.cache/state` | Where memory and backlog are stored. |
| `EV_LLM_STREAMING` | `true` | Start speaking before the model finishes. |
| `EV_TEXT_MODE` | `false` | Typed input instead of the microphone. |

> [!NOTE]
> Windows user folders are resolved through the registry, not by assuming
> `~/Desktop`. OneDrive redirection means `~/Documents` and
> `~/OneDrive/Documents` can both exist while only the second is the one
> Explorer actually shows you.

---

## Project layout

<details>
<summary><b>Full file tree</b></summary>

```
ev_core.py            async event loop: capture, transcribe, decide, act, speak
config.py             all configuration, environment-overridable
.env.example          annotated template — copy to .env
requirements.txt      ten packages
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
  computer_use.py     the eyes and hands: screenshots, vision, mouse, keys
  browser_automation.py  browser_task — Playwright, DOM-level web work
  mission.py          agent_task — the autonomous loop above the others
  web_agent.py        the same loop through the DOM, with no vision cost
  overlay.py          the takeover overlay and the global kill switch
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
  test_computer_use.py       screen tools, coordinates, and the click gate
  test_browser_automation.py the step DSL, the gate, and teardown
  test_open_routing.py       opening folders, mail, and no false successes
  test_compound_requests.py  the second half of a two-part request
  test_mission.py            autonomous runs, scoped consent, kill switch
  test_web_agent.py          the DOM loop: page reading, actions, planner
  test_web_agent_live.py     a real browser on real sites (opt-in)
```

</details>

---

## Testing

```bash
python -m pytest tests/ -q                       # full suite, 321 tests
python -m pytest tests/test_file_manager.py -q   # one file
python -m pytest tests/test_smoke.py -k safety   # one group
```

Every test runs **offline** with no API key, opens no windows, and touches no
real user directory — the file tests redirect `FILE_ROOTS` at a temporary tree.

Three suites are load-bearing rather than incidental:

- **`test_speech_purity.py`** — the regression suite for E.V. reading labels
  aloud. It covers both the structural cause (a tool observation stored as an
  assistant turn) and the boundary that strips labels regardless of origin. Its
  *keep*-cases matter as much as its *strip*-cases: a stripper aggressive enough
  to eat "Spoke to your mother" would be its own bug.
- **`test_file_manager.py` / `test_file_batch.py`** — the containment guarantee.
  A path that escapes `FILE_ROOTS` must be refused, never silently retargeted,
  and a batch action checks every file it touches.
- **`test_backlog.py`** — the rule that keeps a saved command from becoming a
  standing permission.

> There is no `conftest.py`. Each test module bootstraps itself with the same
> three-line preamble before importing `config`, which is what the `# noqa: E402`
> markers are for. New test files need it too.

---

## Requirements

Python 3.10+, a microphone, and one free API key.

| Package | Why |
|---|---|
| `httpx` | Every network call. One pooled async client. |
| `edge-tts` | Microsoft neural voices, synthesised server-side. |
| `sounddevice` + `numpy` | Microphone capture and buffer handling. |
| `rich` | The terminal UI. Optional — without it everything renders as plain text. |
| `pyautogui` | Keystrokes for the VS Code integrated terminal. |
| `pynput` | The global push-to-talk hotkey. |
| `send2trash` | Makes a confirmed delete recoverable. Strongly recommended. |

---

## Platform

**Windows is the primary target.** The core loop, brain, STT and TTS are
portable. These parts are Windows-specific, and each has a documented fallback:

- the `winmm` MP3 player
- the App Paths registry lookup
- the Start Menu shortcut index
- user-folder resolution (OneDrive redirection)
- `dev_workflow`'s integrated-terminal path

> [!IMPORTANT]
> Nothing shells out to `cmd /c start`. For a name Windows cannot resolve,
> `start` pops a **modal error dialog and blocks** until it is dismissed — so an
> unknown app cost a ten-second freeze and an on-screen window before failing
> anyway. E.V. calls `ShellExecuteExW` through `ctypes` instead, for one reason:
> the `SEE_MASK_FLAG_NO_UI` flag. Unknown apps now fail in ~0.15 s with nothing
> on screen.

---

<div align="center">
<sub>Built to run comfortably on a 4 GB machine.</sub>
</div>
