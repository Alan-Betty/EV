# E.V. — Quickstart

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
cd EV
python -m pip install -r requirements.txt
```

No audio package is needed for playback on Windows — E.V. drives the operating
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
EV_LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
EV_STT_PROVIDER=google      # Gemini has no Whisper endpoint; use the free one
```

## 4. Check the setup

```bash
python ev_core.py --check
```

This reports what is working and what is missing, and exits. Fix anything
marked `MISS`. Lines marked `WARN` are optional features degrading gracefully —
for example, no Claude CLI just means `dev_workflow` opens VS Code and stops
there.

## 5. Run it

```bash
python ev_core.py
```

E.V. calibrates to your room noise for a second, then listens. Say **"E.V."**
once to start — after that, just talk.

```
Listening. Say 'ev' to start. Ctrl+C to quit.
Once we're talking you can drop the name for 75s. Say 'take five' to pause me.

you  > hey EV open Chrome and look for a good gaming mouse
E.V. > On it.
you .> what's my python version          <- no "E.V." needed
E.V. > Python 3.13.2
you .> thanks
E.V. > Mm-hm.
```

The `.` in the prompt means the conversation is open. The window resets on
every exchange, so a real back-and-forth never lapses mid-flow — it only
closes after you have actually stopped talking for a while. Tune it with
`EV_CONVERSATION_WINDOW_S`, or say "take five" to end it immediately.

---

## Other ways to run it

```bash
python ev_core.py --text                  # type instead of talking
python ev_core.py --say "open notepad"    # one command, then exit
python ev_core.py --check                 # setup report
python ev_core.py -v                      # verbose logging when something is off
```

`--text` is the fastest way to confirm the brain and the tools work before you
start debugging a microphone.

---

## Sample voice commands

**Launching apps**
- "E.V., open Chrome"
- "E.V., launch VS Code"
- "Hey E.V., open Task Manager"
- "E.V., fire up Spotify"

**Searching the web**
- "E.V., open Chrome and look for a good ergonomic mouse"
- "E.V., search Amazon for a mechanical keyboard under a hundred dollars"
- "E.V., look up async context managers on Stack Overflow"
- "Hey E.V., find me the Rust book on GitHub"
- "E.V., pull up YouTube and search for lockpicking tutorials"

**Developer workflow**
- "E.V., launch VS Code and start Claude Code"
- "E.V., open my EV project in VS Code and start Claude"
- "Hey E.V., open Claude Code in my API project and tell it to review the auth middleware"

**Shell commands**
- "E.V., what's my IP address"
- "E.V., check git status"
- "E.V., what version of Python am I running"
- "E.V., start the dev server in the background"

**Conversation**
- "E.V., what's the difference between a thread and a process?"
- "E.V., how are we doing on time?"
- "E.V., thanks"

**Control phrases — instant, no network**

These never reach the language model, so they land immediately, including
while E.V. is mid-sentence or mid-task.

| Say | E.V. does |
|---|---|
| "E.V., take five" / "hold on" / "one sec" / "quiet" | Goes to standby. Stops talking, ignores everything until you call it back. |
| "wake up" / "back to work" / "I'm back" / "let's go" | Comes back. No wake phrase needed. |
| "stop" / "cancel that" / "never mind" | Drops whatever it was about to do, stays awake. |
| "goodbye" / "shut down" / "that's all" | Exits. |
| "you there?" / "status" | Confirms it is listening. |

```
you  > E.V., take five
E.V. > Standing by.
you  > open chrome                 <- ignored, E.V. is asleep
you  > wake up
E.V. > Back.
```

Standby is a real state, not a pause in the conversation: while asleep, only
"wake up" and "shut down" get through. Everything else is treated as you
talking to someone who is not E.V.

You can also just talk over E.V. while it is speaking — it stops and listens.

---

## Safety

E.V. will run shell commands, which means a misheard word could otherwise
become an executed command. Two layers stop that:

**Blocked outright.** Disk formatting, `rm -rf /`, shadow-copy deletion,
piping a download straight into a shell, fork bombs, boot configuration. These
never run, and no confirmation will unlock them.

**Held for confirmation.** Anything that deletes, moves, kills processes,
installs software, touches the registry, shuts the machine down, or does
something irreversible in git. E.V. tells you what the command would do and
waits for a clear "yes". Silence, ambiguity, or anything that is not plainly
affirmative counts as no.

```
you  > EV delete the old build folder
E.V. > That deletes files. Confirm?
you  > yes
E.V. > Done, no output.
```

To make everything ask first, set `EV_SHELL_CONFIRM_ALL=true`. To switch shell
access off entirely, set `EV_ALLOW_SHELL=false`.

---

## Tuning

**It keeps triggering on background noise.** Raise `EV_VAD_THRESHOLD` in `.env`
(try `0.03`, then `0.05`). E.V. measures your room at startup, so also make sure
you are quiet during the "Calibrating" second.

**It never hears me.** Lower `EV_VAD_THRESHOLD` to `0.01`. Check the right
microphone is selected — `EV_INPUT_DEVICE` accepts a device index or any part
of its name, e.g. `EV_INPUT_DEVICE=Headset`.

**It cuts me off mid-sentence.** Raise `EV_SILENCE_HANG_MS` to `1200`.

**It waits too long after I finish.** Lower `EV_SILENCE_HANG_MS` to `500`.

**It answers when I wasn't talking to it.** That is the conversation window
doing its job a little too well. Lower `EV_CONVERSATION_WINDOW_S` (try `20`),
or set it to `0` to require the wake phrase on every single sentence. Saying
"take five" closes the conversation instantly whenever you need the room back.

**There is a gap before E.V. starts speaking.** Most of it is the ~0.75s round
trip to Microsoft's voice service. Three things already cut it down:

* Stock replies ("Standing by.", "Mm-hm.") are cached on disk, so they start in
  under a tenth of a second.
* Long replies lead with a short opening chunk, so speech begins while the rest
  is still synthesising.
* The Windows MP3 decoder is loaded at startup rather than on your first reply.

If you want to go further, shorten replies — `EV_TTS_FIRST_CHUNK_CHARS` sets how
much E.V. synthesises before starting to talk.

**Change the voice.** The default is `en-US-AvaMultilingualNeural` — female,
conversational, and the most natural of the current Edge voices.

```bash
python -m ev.tts_voices                                 # list English voices
python -m ev.tts_voices --demo en-US-AriaNeural         # hear one
```

Then set `EV_TTS_VOICE` in `.env`.

| Voice | Character |
|---|---|
| `en-US-AvaMultilingualNeural` | Default. Warm, expressive, natural. |
| `en-US-AriaNeural` | Confident, brighter, slightly more clipped. |
| `en-US-EmmaMultilingualNeural` | Softer, cheerful. |
| `en-GB-SoniaNeural` | British. |
| `en-US-GuyNeural` | Male, dry. The previous default. |

All of them synthesise in about 0.75s, so the choice costs nothing in speed.

**Replies are too long.** The system prompt caps E.V. at one or two sentences.
`EV_TTS_CHUNK_CHARS` is a *chunk* size, not a limit — E.V. never drops words,
it splits long replies on sentence boundaries and speaks them all. Lowering it
makes speech start sooner, not finish earlier.

**Change the personality.** The whole persona lives in one string,
`SYSTEM_PROMPT` in [config.py](config.py). Edit it there, or override it
entirely from `.env` with `EV_SYSTEM_PROMPT=...`. The tone examples near the
bottom of that prompt do most of the work — change those and the voice changes.

**It talks over me / won't let me interrupt.** Barge-in is on by default. If
you are on loudspeakers rather than headphones, E.V. can hear itself; raise
`EV_BARGE_IN_FRAMES` (try `14`) or set `EV_TTS_BARGE_IN=false`.

**Speech recognition gets words wrong.** Add the words it keeps missing to
`EV_STT_VOCABULARY` in `.env`. Whisper conditions on that text, so listing your
project names, tools and jargon there fixes most recurring mistakes. Also make
sure `EV_GROQ_STT_MODEL=whisper-large-v3` rather than the turbo variant —
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

**`GROQ_API_KEY is not set`** — you copied `.env.example` but did not paste a
key, or you have a `.env.example` where `.env` should be.

**`No microphone backend`** — `pip install sounddevice`. If PortAudio will not
cooperate, `pip install PyAudio` works as an alternative backend. Or skip the
microphone entirely with `--text`.

**`No audio playback backend`** (non-Windows) — install `ffmpeg` or `mpv`, or
set `EV_TTS_ENABLED=false` to run text-only.

**Rate limited** — free tiers have per-minute caps. Wait a few seconds. If it is
persistent, `llama-3.1-8b-instant` has a much higher limit and is still good
enough for routing to tools.

**Every command answers with an error, and the key is definitely right** — the
model name is probably not available on your account. Groq's catalogue differs
per account, so a name from the docs can still 404. `python ev_core.py --check`
now reports this directly, and E.V. falls back automatically at startup:

```
WARN model: 'llama-3.3-70b-versatile' unavailable; will fall back to 'openai/gpt-oss-120b'
```

To silence the warning, set the working model in `.env`:

```ini
EV_GROQ_MODEL=openai/gpt-oss-120b
```

**E.V. doesn't react at all — the transcript prints but nothing happens** — the
wake phrase was not recognised. E.V. now tells you when it hears something
close to its name. If it happens often, add whatever your microphone actually
produces to `EV_WAKE_PHRASES`, or set `EV_WAKE_REQUIRED=false` to drop the wake
word entirely.

**"Chrome isn't installed" but it is** — E.V. looks on `PATH` and then in the
Windows App Paths registry. An unusual install location can defeat both; add
the full path under `APP_ALIASES` in `config.py`.

**`dev_workflow` opens VS Code but Claude starts in a separate window** — that
is the deliberate fallback. Typing into VS Code's integrated terminal requires
its window to take focus, and if E.V. cannot confirm that it refuses to send
keystrokes rather than risk typing into whatever else was on screen.

---

## Running the tests

```bash
python tests/test_smoke.py    # tools, safety, wake phrase — no network
python tests/test_brain.py    # API wire formats, event loop — mocked, no key
```

Both run fully offline and open no windows.
