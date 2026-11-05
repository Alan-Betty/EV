# L.E.O. — Quickstart

**E**veryday **V**irtual assistant. A desktop voice assistant that actually
controls your machine: opens apps, runs searches, spawns terminals, and starts
Claude Code on a project — from a spoken sentence.

It stays small by putting the expensive parts in the cloud. The language model,
the speech recogniser and the voice synthesiser all run on someone else's
hardware, on free tiers. Locally you are running a Python event loop and a
microphone stream, which is why this works on a 4 GB machine.

---

## 1. Install

You need **Python 3.10 or newer** (3.13 is fine) and a microphone.

```bash
cd Leo
python -m pip install -r requirements.txt
```

No audio package is needed for playback on Windows — Leo drives the operating
system's own MP3 decoder. On Linux or macOS, install `ffmpeg` or `mpv`.

## 2. Get an API key

Pick one. Both have a free tier that comfortably covers personal use.

| Provider | Where | Put it in `.env` as |
|---|---|---|
| **Groq** (recommended — fastest) | <https://console.groq.com/keys> | `GROQ_API_KEY` |
| Google Gemini | <https://aistudio.google.com/apikey> | `GEMINI_API_KEY` |

Groq is the default because it is noticeably quicker, and the same key also
powers speech recognition (Whisper), so one key covers everything.

## 3. Configure

```bash
cp .env.example .env
```

Open `.env` and paste your key in. That is the only required edit — everything
else has a working default.

If you chose Gemini, also set:

```ini
LEO_LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
LEO_STT_PROVIDER=google      # Gemini has no Whisper endpoint; use the free one
```

## 4. Check the setup

```bash
python leo_core.py --check
```

This reports what is working and what is missing, and exits. Fix anything
marked `MISS`. Lines marked `WARN` are optional features degrading gracefully —
for example, no Claude CLI just means `dev_workflow` opens VS Code and stops
there.

## 5. Run it

```bash
python leo_core.py
```

Leo calibrates to your room noise for a second, then listens. Say **"Leo"**
once to start — after that, just talk.

```
Listening. Say 'ev' to start. Ctrl+C to quit.
Once we're talking you can drop the name for 75s. Say 'take five' to pause me.

you  > hey Leo open Chrome and look for a good gaming mouse
Leo > On it.
you .> what's my python version          <- no "Leo" needed
Leo > Python 3.13.2
you .> thanks
Leo > Mm-hm.
```

The `.` in the prompt means the conversation is open. The window resets on
every exchange, so a real back-and-forth never lapses mid-flow — it only
closes after you have actually stopped talking for a while. Tune it with
`LEO_CONVERSATION_WINDOW_S`, or say "take five" to end it immediately.

---

## The terminal UI

Leo renders in a sci-fi console built on `rich`: an ASCII header, colour-coded
turns, and an animated status line that tells you which stage it is in.

```
 ███████╗    ██╗   ██╗
 ██╔════╝    ██║   ██║
 █████╗      ██║   ██║
 ██╔══╝      ╚██╗ ██╔╝
 ███████╗██╗  ╚████╔╝ ██╗
 ╚══════╝╚═╝   ╚═══╝  ╚═╝
   EVERYDAY VIRTUAL ASSISTANT
   cloud brain, local hands

   brain  groq  llama-3.3-70b-versatile
   voice  en-US-GuyNeural
   mode   voice
```

| Colour | Means |
|---|---|
| dim `you >` + bright white | what you said |
| cyan panel | what Leo is saying |
| amber `⚙ tool_name` | a tool actually running |
| dim `·` | a note from Leo about itself |
| red `✗` | something failed |

The status line cycles through `Listening...`, `Transcribing...`,
`Thinking...`, `Executing...` as the turn progresses.

**The UI cannot change what Leo says.** This is structural, not a convention:
every function in `leo/ui.py` returns `None`, so there is no path by which a
label it draws could be handed to the speaker. The reply text reaches
`Speaker.say` on a completely separate line in `leo_core.py`. A test asserts
the `None` return for every renderer, so it stays true.

If `rich` is not installed, stdout is redirected to a file, or you set
`LEO_UI_PLAIN=true`, the whole thing degrades to plain text automatically. Set
`LEO_UI_SPINNERS=false` to keep the colour but drop the animation.

---


## Other ways to run it

```bash
python leo_core.py --text                  # type instead of talking
python leo_core.py --say "open notepad"    # one command, then exit
python leo_core.py --check                 # setup report
python leo_core.py -v                      # verbose logging when something is off
```

`--text` is the fastest way to confirm the brain and the tools work before you
start debugging a microphone.

---

## Sample voice commands

**Launching apps**
- "Leo, open Chrome"
- "Leo, launch VS Code"
- "Hey Leo, open Task Manager"
- "Leo, fire up Spotify"

**Searching the web**
- "Leo, open Chrome and look for a good ergonomic mouse"
- "Leo, search Amazon for a mechanical keyboard under a hundred dollars"
- "Leo, look up async context managers on Stack Overflow"
- "Hey Leo, find me the Rust book on GitHub"
- "Leo, pull up YouTube and search for lockpicking tutorials"

**File control**

Leo has real access to your files, scoped to your user folders. Say the
folder name the way you would to a person — "Documents", "my Downloads
folder", "the Desktop".

- "Leo, create a txt file in Documents with the 10 most visited places"
- "Leo, copy report.pdf from Downloads to Documents"
- "Leo, what's in my Downloads folder"
- "Leo, organise my Downloads folder"
- "Leo, rename that to quarterly-notes.txt"
- "Leo, find anything with 'invoice' in Downloads"
- "Leo, make a folder called screenshots on my Desktop"
- "Leo, read me the top of my todo file"
- "Leo, delete the old zip in Downloads"   ← asks first

Creating a file writes the *actual* content you asked for, not a placeholder:

```
you  > Leo, create a txt file in Documents called places with the 10 most visited
  ⚙ file_manager  action=create  path=Documents/places.txt
Leo > Made places.txt in Documents.
```

Deleting and organising always ask before they run:

```
you  > Leo, delete the old zip in Downloads
Leo > That'll delete archive.zip in Downloads. Sure?
you  > yeah
Leo > archive.zip in Downloads is in the Recycle Bin.
```

Two things keep this safe. Everything resolves inside `LEO_FILE_ROOTS` (your
user profile by default), so a misheard word cannot reach `C:\Windows`. And
deletes go to the Recycle Bin when `send2trash` is installed, so a mistaken
"yes" is recoverable.


**Developer workflow**
- "Leo, launch VS Code and start Claude Code"
- "Leo, open my Leo project in VS Code and start Claude"
- "Hey Leo, open Claude Code in my API project and tell it to review the auth middleware"

**Shell commands**
- "Leo, what's my IP address"
- "Leo, check git status"
- "Leo, what version of Python am I running"
- "Leo, start the dev server in the background"

**Conversation**
- "Leo, what's the difference between a thread and a process?"
- "Leo, how are we doing on time?"
- "Leo, thanks"

**Control phrases — instant, no network**

These never reach the language model, so they land immediately, including
while Leo is mid-sentence or mid-task.

| Say | Leo does |
|---|---|
| "Leo, take five" / "hold on" / "one sec" / "quiet" | Goes to standby. Stops talking, ignores everything until you call it back. |
| "wake up" / "back to work" / "I'm back" / "let's go" | Comes back. No wake phrase needed. |
| "stop" / "cancel that" / "never mind" | Drops whatever it was about to do, stays awake. |
| "goodbye" / "shut down" / "that's all" | Exits. |
| "you there?" / "status" | Confirms it is listening. |

```
you  > Leo, take five
Leo > Standing by.
you  > open chrome                 <- ignored, Leo is asleep
you  > wake up
Leo > Back.
```

Standby is a real state, not a pause in the conversation: while asleep, only
"wake up" and "shut down" get through. Everything else is treated as you
talking to someone who is not Leo

You can also just talk over Leo while it is speaking — it stops and listens.

---

## WhatsApp

L.E.O. uses [`wacli`](https://github.com/openclaw/wacli), an unofficial linked-device client. **Using it can break WhatsApp's terms, and your number could be banned.** L.E.O. keeps the risk down but cannot remove it. If you have a spare number, try that first.

1. Install `wacli` and put it on your `PATH`.
2. Run `wacli auth` and scan the code on your phone, the way you link WhatsApp Web.
3. Run `python leo_core.py --check`. It should say `wacli paired`.

What L.E.O. does to look like a person rather than a bot:

- Every send reads out the contact and the exact text, then waits for your spoken yes.
- It only messages one person you already have a chat with. No strangers, groups or channels.
- At most 10 messages an hour and 30 a day, at least 30 seconds apart, and the same text never twice in an hour. These are cautious guesses, since WhatsApp publishes no safe rate. Change them with `LEO_WHATSAPP_*`.
- It never retries. If a send fails, it may still have arrived, so check your phone.
- If WhatsApp signs you out or reports a ban, or two sends fail in a row, sending stops until you run `python leo_core.py --whatsapp-reset`.

Message text and examples of your own messages go to your cloud model (Groq or Gemini) when L.E.O. reads or drafts. Files reach your phone through KDE Connect, so pair the phone there first.

## Memory by person and topic

Say who or what something concerns: "remember Priya is allergic to peanuts", "Priya is Sam's sister", "for the garden project, the shed needs a new roof". When a name comes up later, its notes and links ride along for that turn. "Forget everything about Priya" asks first, then removes that branch. Notes about people are not sent to the embedding service unless you set `LEO_MEMORY_TREE_EMBED_PEOPLE=true`.

After L.E.O. reads outside text (a page, a file, a message), saving a memory asks first for two minutes, so nothing it read can write itself in. Learning from finished conversations is off until you set `LEO_MEMORY_EXTRACT_ENABLED=true`.

## Coding sessions

"Start a coding session to fix the failing test in my project" runs Claude Code on its own, in a new git worktree and branch under `.cache/state/worktrees`. It asks first, stops at `LEO_DEV_MAX_BUDGET_USD` (2 dollars by default), never pushes or merges, and tells you when it stops. "Stop the coding session" or Ctrl+Alt+Q ends it.

L.E.O. then runs the tests itself: `LEO_DEV_TEST_COMMAND` if set, otherwise chosen from what is in the repo. Those tests run project code with no sandbox, as they would for you. Your branch is yours to review; delete it and the worktree with `git worktree remove` when done.

## Safety

Leo will run shell commands, which means a misheard word could otherwise
become an executed command. Two layers stop that:

**Blocked outright.** Disk formatting, `rm -rf /`, shadow-copy deletion,
piping a download straight into a shell, fork bombs, boot configuration. These
never run, and no confirmation will unlock them.

**Held for confirmation.** Anything that deletes, moves, kills processes,
installs software, touches the registry, shuts the machine down, or does
something irreversible in git. Leo tells you what the command would do and
waits for a clear "yes". Silence, ambiguity, or anything that is not plainly
affirmative counts as no.

```
you  > Leo delete the old build folder
Leo > That deletes files. Confirm?
you  > yes
Leo > Done, no output.
```

To make everything ask first, set `LEO_SHELL_CONFIRM_ALL=true`. To switch shell
access off entirely, set `LEO_ALLOW_SHELL=false`.

---

## Tuning

**It keeps triggering on background noise.** Raise `LEO_VAD_THRESHOLD` in `.env`
(try `0.03`, then `0.05`). Leo measures your room at startup, so also make sure
you are quiet during the "Calibrating" second.

**It never hears me.** Lower `LEO_VAD_THRESHOLD` to `0.01`. Check the right
microphone is selected — `LEO_INPUT_DEVICE` accepts a device index or any part
of its name, e.g. `LEO_INPUT_DEVICE=Headset`.

**It cuts me off mid-sentence.** Raise `LEO_SILENCE_HANG_MS` to `1200`.

**It waits too long after I finish.** Lower `LEO_SILENCE_HANG_MS` to `500`.

**It answers when I wasn't talking to it.** That is the conversation window
doing its job a little too well. Lower `LEO_CONVERSATION_WINDOW_S` (try `20`),
or set it to `0` to require the wake phrase on every single sentence. Saying
"take five" closes the conversation instantly whenever you need the room back.

**There is a gap before Leo starts speaking.** Most of it is the ~0.75s round
trip to Microsoft's voice service. Three things already cut it down:

* Stock replies ("Standing by.", "Mm-hm.") are cached on disk, so they start in
  under a tenth of a second.
* Long replies lead with a short opening chunk, so speech begins while the rest
  is still synthesising.
* The Windows MP3 decoder is loaded at startup rather than on your first reply.

**Where the time actually goes.** Measured on this setup against the live APIs,
for one short spoken reply:

| Stage | Cost | Notes |
|---|---|---|
| Groq, first token | 1.1 – 1.45s | dominant, and essentially fixed |
| Groq, rest of the reply | ~0.03s | generation is not the bottleneck |
| edge-tts synthesis | ~0.7s + 0.004s/char | 12 ch 0.88s · 45 ch 1.06s · 90 ch 1.23s · 180 ch 1.41s |
| edge-tts, cache hit | 0.002s | a file copy |

Two things follow from this, and they are worth knowing before you spend time
tuning the wrong knob:

* **The cache is the biggest win by far**, which is why every stock reply is
  pre-synthesised at startup. A cached reply is ~400× faster than a fresh one.
* **Shortening the first chunk helps, but modestly.** Dropping
  `LEO_TTS_FIRST_CHUNK_CHARS` from 90 to 60 buys about 0.2s before the first
  word. It is set to 60 by default for that reason. Single long sentences are
  never split mid-sentence — the seam would cost more than the wait.

**About LLM streaming.** `LEO_LLM_STREAMING=true` makes Leo start speaking on
the first finished sentence rather than the last token. Be aware of what this
does and does not buy you: **on Groq function calls it currently buys nothing**,
because Groq delivers the whole tool-call argument blob in a single SSE frame
(measured: 1 delta, 0.0s spread). It does help when the model answers with
prose instead of a tool call, where content streams in ~19 frames. It costs
nothing when it does not help, and falls back to a plain request on any error,
so it is left on. If a future model streams tool arguments incrementally, the
machinery is already in place.


**Change the voice.** The default is `en-US-GuyNeural` — male,
conversational, and quick enough to carry Leo's deadpan.

```bash
python -m leo.tts_voices                                 # list English voices
python -m leo.tts_voices --demo en-US-AriaNeural         # hear one
```

Then set `LEO_TTS_VOICE` in `.env`.

| Voice | Character |
|---|---|
| `en-US-AvaMultilingualNeural` | Default. Warm, expressive, natural. |
| `en-US-AriaNeural` | Confident, brighter, slightly more clipped. |
| `en-US-EmmaMultilingualNeural` | Softer, cheerful. |
| `en-GB-SoniaNeural` | British. |
| `en-US-GuyNeural` | Male, dry. The previous default. |

All of them synthesise in about 0.75s, so the choice costs nothing in speed.

**Replies are too long.** The system prompt caps Leo at one or two sentences.
`LEO_TTS_CHUNK_CHARS` is a *chunk* size, not a limit — Leo never drops words,
it splits long replies on sentence boundaries and speaks them all. Lowering it
makes speech start sooner, not finish earlier.

**Change the personality.** The whole persona lives in one string,
`SYSTEM_PROMPT` in [config.py](config.py). Edit it there, or override it
entirely from `.env` with `LEO_SYSTEM_PROMPT=...`. The tone examples near the
bottom of that prompt do most of the work — change those and the voice changes.

**It talks over me / won't let me interrupt.** Barge-in is on by default. If
you are on loudspeakers rather than headphones, Leo can hear itself; raise
`LEO_BARGE_IN_FRAMES` (try `14`) or set `LEO_TTS_BARGE_IN=false`.

**Speech recognition gets words wrong.** Add the words it keeps missing to
`LEO_STT_VOCABULARY` in `.env`. Whisper conditions on that text, so listing your
project names, tools and jargon there fixes most recurring mistakes. Also make
sure `LEO_GROQ_STT_MODEL=whisper-large-v3` rather than the turbo variant —
turbo is faster but noticeably less accurate on short commands.

---

## What runs where

| Piece | Where it runs | Local cost |
|---|---|---|
| Language model | Groq / Gemini | one HTTPS request |
| Speech recognition | Groq Whisper | one HTTPS upload |
| Voice synthesis | Microsoft Edge TTS | one WebSocket, a temp MP3 |
| Audio playback | Windows `winmm` (built in) | none |
| Wake phrase | string match on the transcript | none |
| Automation | local Python | `subprocess`, `ctypes` |

Resident memory in practice is roughly **90–160 MB**, most of it the Python
interpreter and the audio buffer.

Latency per command, once warm: about **0.5 s** to transcribe, **0.5–1.5 s** to
think, **0.8 s** to synthesise speech, then playback. The first command after
startup is not slower — the TTS stack is warmed up while the microphone
calibrates.

---

## Troubleshooting

**Leo reads a label out loud** — "Spoke: Chrome's up", "Leo: Chrome's up".
This was a real bug and it is fixed in two independent places, so it should not
come back:

* *The cause.* The `chat` tool used to report its result as `Spoke: <text>`,
  and the core loop stored that string as an **assistant** turn. The model then
  saw its own previous replies prefixed with a label, treated it as the house
  style, and started writing `Spoke:` into text that went straight to the
  speaker. Tool results now travel in a separate `observation` field that is
  replayed in an input role, never as something Leo said.
* *The backstop.* `clean_for_speech` strips role labels, UI chrome, shell
  echoes and JSON envelopes from anything on its way to synthesis, however it
  got there. It is deliberately strict about the separator, so "Spoke: hi"
  is stripped and "Spoke to your mother" is not.

If you ever hear one, it means something produced a label shape the stripper
does not know. Add it to `_LABEL` in `leo/tts.py` and add a case to
`tests/test_speech_purity.py`.

**The UI looks like plain text.** `rich` is not installed
(`pip install rich`), stdout is being redirected, or `LEO_UI_PLAIN=true`. None
of these affect behaviour — only appearance.

**"Documents" is not the folder I expected.** On a profile backed by OneDrive,
`C:\Users\you\Documents` and `C:\Users\you\OneDrive\Documents` can both exist,
and only the second is the one Explorer shows you. Leo reads the real location
from the Windows known-folder registry, so "Documents" and "Desktop" resolve to
wherever your profile actually points — OneDrive included. Check what it
resolved to:

```bash
python -c "import config; [print(k, v) for k, v in config.USER_DIRS.items()]"
```

Override any of them with `LEO_FILE_DEFAULT_DIR`, or widen `LEO_FILE_ROOTS` if
your folders live somewhere unusual.


**"That's outside the folders I'm allowed to touch."** `file_manager` refused a
path outside `LEO_FILE_ROOTS`, which defaults to your user profile. This is the
guard that stops a misheard word reaching system files. If you genuinely want
Leo working elsewhere, add the root explicitly in `.env`:

```bash
LEO_FILE_ROOTS=C:\Users\you;D:\Projects
```

**Deletes are permanent.** Install `send2trash` and they go to the Recycle Bin
instead: `pip install send2trash`. Check with `python leo_core.py --check`.


**`GROQ_API_KEY is not set`** — you copied `.env.example` but did not paste a
key, or you have a `.env.example` where `.env` should be.

**`No microphone backend`** — `pip install sounddevice`. If PortAudio will not
cooperate, `pip install PyAudio` works as an alternative backend. Or skip the
microphone entirely with `--text`.

**`No audio playback backend`** (non-Windows) — install `ffmpeg` or `mpv`, or
set `LEO_TTS_ENABLED=false` to run text-only.

**Rate limited** — free tiers have per-minute caps. Wait a few seconds. If it is
persistent, `llama-3.1-8b-instant` has a much higher limit and is still good
enough for routing to tools.

**Every command answers with an error, and the key is definitely right** — the
model name is probably not available on your account. Groq's catalogue differs
per account, so a name from the docs can still 404. `python leo_core.py --check`
now reports this directly, and Leo falls back automatically at startup:

```
WARN model: 'llama-3.3-70b-versatile' unavailable; will fall back to 'openai/gpt-oss-120b'
```

To silence the warning, set the working model in `.env`:

```ini
LEO_GROQ_MODEL=openai/gpt-oss-120b
```

**Leo doesn't react at all — the transcript prints but nothing happens** — the
wake phrase was not recognised. Leo now tells you when it hears something
close to its name. If it happens often, add whatever your microphone actually
produces to `LEO_WAKE_PHRASES`, or set `LEO_WAKE_REQUIRED=false` to drop the wake
word entirely.

**"Chrome isn't installed" but it is** — Leo looks on `PATH` and then in the
Windows App Paths registry. An unusual install location can defeat both; add
the full path under `APP_ALIASES` in `config.py`.

**`dev_workflow` opens VS Code but Claude starts in a separate window** — that
is the deliberate fallback. Typing into VS Code's integrated terminal requires
its window to take focus, and if Leo cannot confirm that it refuses to send
keystrokes rather than risk typing into whatever else was on screen.

---

## Running the tests

```bash
python -m pytest tests/ -q                      # all 130
python -m pytest tests/test_speech_purity.py -q  # the no-labels guarantee
python -m pytest tests/test_file_manager.py -q   # file ops + root containment
```

| Suite | Covers |
|---|---|
| `test_smoke.py` | tools, safety classifier, wake phrase |
| `test_brain.py` | Groq and Gemini wire formats, history bounds |
| `test_speech_purity.py` | no label ever reaches the speaker, from either cause |
| `test_file_manager.py` | file operations, and paths outside the roots being refused |
| `test_streaming_and_ui.py` | partial-JSON reply parsing, UI/speech separation |
| `test_stream_integration.py` | SSE frames in, spoken sentences out |

All of them run fully offline, need no API key, and open no windows. The file
tests redirect `FILE_ROOTS` at a temporary tree, so a bug in the tool cannot
reach the machine running the suite.
