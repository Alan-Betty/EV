# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m pip install -r requirements.txt

python ev_core.py --check          # config readiness report; exit 1 if problems
python ev_core.py                  # voice loop
python ev_core.py --text           # typed input, same brain and tools
python ev_core.py --say "open notepad"   # one command, then exit
python ev_core.py -v               # debug logging

python -m pytest tests/ -q                       # full suite, 419 tests, offline
python -m pytest tests/test_file_manager.py -q   # one file
python -m pytest tests/test_smoke.py -k safety   # one test or group

python -m ev.tts_voices                          # list voices
python -m ev.tts_voices --demo en-US-AriaNeural  # audition one
```

`--check` is the fastest way to diagnose a broken environment: it verifies the API key, confirms the Groq model exists on this account, names an input device, prints the resolved `FILE_ROOTS`, and confirms the state directory is writable (a read-only one means a silently forgetful assistant).

There is no `conftest.py`. Every test module bootstraps itself with the same preamble — `sys.path.insert(0, parent)`, `os.environ.setdefault("GROQ_API_KEY", ...)`, `os.environ["EV_TTS_ENABLED"] = "false"` — before importing `config`. New test files need it too, and it must run before any project import, hence the `# noqa: E402` markers.

## Architecture

One async loop in [ev_core.py](ev_core.py) owns everything: `listen → transcribe → local intent? → decide → act → speak`. Every blocking call (microphone reads, `subprocess`, keystrokes, tool dispatch) is pushed through `asyncio.to_thread`, so a slow tool never stalls the loop or blocks Ctrl+C. New blocking work must follow that rule.

`EV.__init__` creates one `httpx.AsyncClient` shared by `Brain` and `Transcriber`. That sharing is deliberate — it keeps TLS connections warm across utterances — so don't introduce per-call clients.

### Dependency policy

The project's core constraint is resident memory (~90–160 MB). It runs no local model: the LLM and Whisper are HTTP calls, TTS is edge-tts, MP3 playback is Windows `winmm` over `ctypes`, and the wake word is a string match on the transcript. There are deliberately no vendor SDKs (`groq`, `google-generativeai`, `openai`) and no audio libraries (`pygame`, `pydub`, `ffmpeg`). Both providers are a single JSON POST built by hand in [ev/brain.py](ev/brain.py). Adding a heavyweight dependency undoes the whole design; reach for `httpx` and the stdlib first.

Screen perception does not change that rule, it follows it. There is no local
vision model: `mss` grabs the frame, Pillow downscales it and JPEGs it, and
the result is posted to the same endpoint the brain already uses - ~95 KB at
the 1280px a plain look uses, ~160 KB at the 1600px a screen task gets,
because a step that is about to click is worth more pixels than one that is
only being described. Both are imported lazily, so the ~15 MB they cost is
paid from the first screenshot onwards and never in a session that only
talks.

Everything added since for accuracy has stayed inside the rule, and that was
a constraint rather than a coincidence. The obvious way to make a desktop
agent reliable on Windows is UI Automation, which means `comtypes` or
`pywinauto` and the resident cost that comes with them. The window inventory
in [tools/window.py](tools/window.py) gets most of that benefit - titles,
focus, exact rectangles - from `ctypes` against user32 for nothing, and the
coordinate ruler and the region zoom are Pillow calls on a frame that was
already being encoded. No new package was added for any of it.

Playwright is the one genuinely heavy dependency in the tree, which is why it
is optional, imported inside `browser_task`, and torn down in a `finally`
when the task ends - between tasks it costs an unused import path.

### The speech-purity boundary

This is the most load-bearing invariant in the codebase, and it spans four files. E.V. once read "Spoke:" aloud, and the fix had two halves that must both stay intact:

- **Structural.** `Brain.remember(user, assistant, observation)` keeps the assistant role speech-only. Machine detail — exit codes, "Launched chrome.exe" — goes in `observation` and is replayed in an input role (a `system` message for Groq, a `user` turn for Gemini). Storing a tool observation as an assistant turn teaches the model, by example, to prefix its own replies with labels. `ToolResult.speech` is what gets said; `ToolResult.detail` is what the model sees next turn.
- **Boundary.** `clean_for_speech` in [ev/tts.py](ev/tts.py) strips label-shaped prefixes regardless of origin, then strips again after markdown removal (`**E.V.:** hi` hides a label behind markdown). It must not become aggressive enough to eat real speech like "Spoke to your mother" — `tests/test_speech_purity.py` pins both the strip-cases and the keep-cases.

[ev/ui.py](ev/ui.py) is a strict dead end supporting this: every function returns `None` and only draws. Labels, panels and spinners live there and nowhere else, and the reply text reaches `Speaker.say` by a separate path in `ev_core`. Never have a UI function return a string that flows toward the speaker.

### Tool contract

[tools/schemas.py](tools/schemas.py) is the single source of truth. `TOOL_SPECS` is provider-neutral JSON Schema; `to_openai_tools()` and `to_gemini_tools()` translate it, so a tool is described exactly once. Gemini rejects schema keys outside its OpenAPI subset — `test_smoke.py` asserts this.

`dispatch` in [tools/\_\_init\_\_.py](tools/__init__.py) is the only entry point. It filters arguments down to `_ALLOWED_ARGS`, which is derived from `TOOL_SPECS` so the two cannot drift, coerces the loose types LLMs emit, and never raises — a broken tool returns a failure `ToolResult` rather than killing the assistant.

Adding a tool means: a spec in `TOOL_SPECS`, an implementation returning `ToolResult`, and an entry in `REGISTRY`. Nothing else, and nothing outside the schema will reach the function.

### A launch that goes nowhere must not report success

"Open File Explorer and go to my GitHub folder" used to do nothing visible,
and the reason is worth keeping written down because it will recur.

There was no tool for it. `file_manager` listed what was *in* a folder;
nothing put one on screen. So the model routed it through `open_app` and
invented an argument - `C:\Users\Alan\GitHub`, on a machine whose user is
not called Alan. Explorer opens its **default location** for a path that does
not exist and exits 0, so `open_app` reported a launch and E.V. said
"Explorer, up." The user was looking at the wrong folder and the model, next
turn, believed it had succeeded.

Three things fix that shape of bug, and all three matter:

- `file_manager` has an `open` action, so the capability exists to route to.
  It checks existence *before* launching and names the folder when it is not
  there, because Explorer at a bad path is indistinguishable from Explorer at
  a good one.
- `open_app` validates any path-shaped argument through
  `file_manager.resolve_user_path` before it reaches a process. A wrong
  absolute path is usually right about its last component, so the basename is
  looked up before giving up; going through `file_manager` also means
  `FILE_ROOTS` applies, so an app cannot be used to open a folder the file
  tools would refuse. Switches (`-f`, `/select,`, `--profile=x`) are
  deliberately not path-checked.
- The failure `detail` names the tool to use instead. Without that the model
  retries the same guess with different spelling.

`open` on a *file* reads it out; `open` on a folder reveals it. Same word
from the user, different job, and the path decides rather than the verb.

The sibling case is `web_search`: "open my email" had nowhere to go, so the
model asked which provider instead of acting. `SEARCH_ENGINES` now holds
destinations as well as searches, and `_is_destination` tells them apart by
whether the template contains `{q}` - so there is no second list to keep in
step. A destination reached with a query still opens the destination, because
an inbox has no search URL and a built one would 404.

### Confirmation and safety

`confirmed` is injected by the core loop after a spoken yes — it is added to `_ALLOWED_ARGS` manually and is never something the model can set for itself. The flow: a tool returns `ToolResult.confirm(...)` → `ev_core._execute` stores `session.pending` (holding the tool name too, since file deletes and shell commands both arrive here) → the next utterance goes to `_resolve_pending`. Only an unambiguous yes runs it; an utterance that is neither yes nor no is treated as the user moving on, and is re-dispatched as a fresh command rather than swallowed.

Two independent gates enforce this:

- [tools/safety.py](tools/safety.py) `classify()` returns `SAFE` / `REVIEW` / `BLOCKED`. Blocked patterns (disk format, pipe-to-shell, shadow-copy deletion, fork bombs) run under no confirmation at all.
- [tools/file_manager.py](tools/file_manager.py) `_check()` resolves every path through `realpath` and refuses anything outside `config.FILE_ROOTS`, raising `PathRefused`. A refused path is never silently retargeted. `tests/test_file_manager.py` and `tests/test_file_batch.py` monkeypatch `FILE_ROOTS` and `USER_DIRS` at a temp tree, so the suite can never touch a real user directory.

The batch actions (`batch_copy`, `batch_move`, `batch_rename`) act on a whole folder at once, so a leaked root would leak by the hundred: every individual source *and* destination goes back through `_check`, not just the two folders named in the call. They glob rather than `rglob` — "the PDFs in Downloads" is not "every PDF under my home folder" — and `batch_move`/`batch_rename` are gated like `delete`. `backlog clear` is gated the same way, which is why `backlog` is in the manual `confirmed` allow-list in `tools/__init__.py`.

`dev_workflow` is gated the same way: it only types into VS Code's integrated terminal once [tools/window.py](tools/window.py) confirms that window holds focus, and otherwise degrades to spawning a separate terminal rather than typing into whatever is on screen.

### Streaming

With `LLM_STREAMING` on and audio enabled, `Brain._decide_groq_streamed` pulls the `chat` reply out of half-written tool-call JSON via `partial_reply()` (hand-rolled, because `json.loads` is useless mid-stream) and hands complete sentences to a `SpeechStream`. **Only `chat` streams** — every other tool has a side effect, and announcing "Chrome's up" before Chrome is up would be a lie. The stream is created lazily on the first sentence and must be `cancel()`ed if the call turns out not to be `chat`, or it holds the speaker lock forever. Streaming is an optimisation and never a dependency: any failure falls back to a plain call.

Tool calls are accumulated **per `index`**, not into one buffer. A model answering "open notepad and type X" emits two calls in one completion, and concatenating their argument fragments produced `{...}{...}` — not valid JSON, so every argument was dropped and the tool ran on nothing. Only the first call is acted on, matching the non-streaming path.

### When the model's own tool call is rejected

Groq returns a 400 with code `tool_use_failed` when the model emits no tool call under `tool_choice: required`, or arguments that are not valid JSON. That is the model stumbling, not the request being wrong, so `BrainError.tool_failure` marks it and it is never surfaced on the first try. A request that mixes a question with an action ("open notepad and type the second largest word in the dictionary") triggers it reliably, and before the ladder below it ended the turn with nothing said and an empty terminal.

Three rungs: `tool_choice: auto`, then `_plain_reply` with the tools stripped entirely (which turns the question into a spoken answer), and only then the error — which is phrased as English because it reaches a speech synthesiser. A raw JSON error body read aloud is the worst possible reply. The streamed path is a fourth case: Groq ends the stream with no frames at all rather than an error, so an empty stream falls back to the plain call instead of answering "I didn't catch that", which blamed the user for the model's stumble.

### Immediate acknowledgement

A tool that launches an app or walks a folder tree takes seconds, and silence for those seconds reads as "it didn't hear me" — so the user repeats themselves and now there are two commands in flight. `EV._start_ack` draws an acknowledgement immediately and speaks one from a **background task**, so the dispatch is already running on its own thread before a syllable comes out. Awaiting the speech before the tool would make every command a second slower and defeat the whole thing.

It is also delayed by `ACK_DELAY_S`: a tool that returns in 200ms needs no "stand by", and `_finish_ack` cancels the task before it speaks. Once it *is* speaking it is allowed to finish — cancelling an `asyncio.to_thread` playback does not stop the OS thread, so cutting in would put E.V. on top of itself. `chat` is never acknowledged; it has no side effect to wait on and is already streaming its real reply.

### Cancelling a running tool

"Stop" used to do nothing once work was underway. The tool sits on a worker thread, the loop is blocked awaiting it, and a thread cannot be killed from outside — so cancellation is **cooperative**: `CancelToken` in [tools/base.py](tools/base.py) is a request the tool honours at a point where stopping is *safe*, between two files or two polls of a subprocess, never mid-write. That restraint is the design, not a limitation of it.

`dispatch(name, arguments, cancel)` attaches the token **after** argument filtering and type coercion — so the model can neither supply one nor clear one, and it never gets stringified on the way through. Only tools in `CANCELLABLE` receive it. `terminal_command` polls `Popen` instead of calling `subprocess.run` (which is precisely why "stop" was inert: `run` blocks the thread with no moment to act on anything) and drains stdout on reader threads, because a full pipe deadlocks a process nobody is reading from. `file_manager` checks between files in the batch actions and `organize`.

While a cancellable tool runs, `EV._watch_for_cancel` keeps the microphone open. Two rules there: an utterance that is **not** a cancel is held in `_queued_utterance` and handled by `_route` afterwards rather than discarded — talking over a slow tool is usually the next command — and a cancel aimed at a tool outside `CANCELLABLE` sets `_cancel_refused`, which E.V. says out loud. `open_app` has already launched the program; reporting a stop that did not happen would be worse than admitting it cannot.

A cancelled tool returns `ToolResult.stopped`, which is `ok=True` (the files that moved really did move) carrying `cancelled` in its data. The core loop reads that and backlogs the remainder as `interrupted`.

### Screen perception and computer use

[tools/computer_use.py](tools/computer_use.py) is E.V.'s eyes and hands, and
[tools/browser_automation.py](tools/browser_automation.py) is the faster route
for anything on a web page. Five tools: `take_screenshot`, `mouse_action`,
`keyboard_action`, `screen_task` and `browser_task`.

**Coordinates are fractions, never pixels.** The model is shown a frame
downscaled to `VISION_MAX_WIDTH`, so a pixel coordinate from it would be wrong
by whatever the scale factor happened to be that time. `_coordinate` reads 0-1
as a fraction of the real screen and anything larger as a pixel, with one
exception worth knowing about: a non-integer just over 1, like `1.02`, is a
fraction that overshot, and reading it as "pixel number one" would put the
click in the top-left corner - the one place on screen where a stray click can
do real damage.

**Three things make a fraction accurate, and none of them is a better
model.** Asked for a coordinate from a bare screenshot, a model estimates one
by eye and lands a few percent out; a few percent of 1920px is a different
menu item.

- `_draw_ruler` overlays a labelled grid before the frame is sent, so the
  model reads a number off the nearest line instead of guessing at one. The
  lines are blended at low alpha on a layer of their own - a solid grid buys
  coordinate accuracy by spending text accuracy, since it sits directly on
  top of the file names it is there to help click.
- `capture_screen(region=...)` crops *before* the downscale. A dialog 400px
  wide on a 4K display reaches the model as 400 real pixels instead of the
  130 that survive squeezing the desktop to 1280. The ruler on a zoomed frame
  is still labelled in **whole-screen fractions**, so a coordinate read off a
  zoom means the same thing as one read off a full frame. That is deliberate:
  the alternative is asking the model to rescale its own answer, which is the
  arithmetic it is worst at.
- `window.list_windows` hands over what the window manager already knows -
  which applications are open, which one has focus, and the exact rectangle
  each owns. A screenshot shows an editor; the inventory says it is VS Code,
  that it is focused, and where it is. It is pure `ctypes`, it costs nothing,
  and it removes the inference the model is least reliable at. Cloaked
  windows and shell furniture (`Progman`, `WorkerW`) are filtered out: they
  are real handles that are not on the screen, and offering one to something
  about to click is worse than offering nothing.

**The loop is the point.** `screen_task` is capture, parse, act, look again.
A single screenshot tells the model where a button is *now*, and by the time
the click lands the screen has moved on, so the second look is what verifies
the first step rather than being an optimisation. Two ceilings bound it:
`SCREEN_TASK_MAX_STEPS` stops a run clicking forever on a page that never
changes, and `SCREEN_TASK_TIMEOUT_S` stops one where each step is merely slow.
Steps are executed by calling `mouse_action` and `keyboard_action`, not by
touching pyautogui directly, so the loop cannot route around their checks.
`launch` and `focus` go through `open_app` and `tools/window.py` for the same
reason - a path-shaped launch argument still meets `FILE_ROOTS` on the way
past.

**A batch may only contain actions whose effect is already known.** The model
may return several actions in one reply, up to `SCREEN_TASK_MAX_BATCH`, which
is what lets "open Notepad and type hello" cost one vision call rather than
four. But `_step_actions` cuts the batch after `launch` or `focus`, and this
is not tidiness. That exact request was planned - correctly - as launch, wait,
type, and on a machine where Notepad was already open on a page of the user's
own notes the "hello" landed in the middle of them. Launching something tells
you it is running. It tells you nothing about what is *in* it, and typing into
a window nobody has looked at is writing into the dark. A trailing `wait` may
ride along, because waiting is how a launch finishes rather than a new thing
being done. The same episode is why the step prompt says to open a new
document when the app comes up showing work that is already there.

**A screen that does not change is information.** `Frame.fingerprint` is a
12x12 average hash, compared with a tolerance of two squares. A model cannot
tell from one frame that it is clicking a dead button, because a dead button
looks exactly like the one it just clicked, so it will click until the step
budget runs out. Comparing consecutive frames is the only place that fact
exists. The coarseness is the design: an exact comparison of two screenshots
is always "different" - a caret, a clock, a hover state - so it would answer
a question nobody asked. One unchanged frame nudges the model; two in a row
ends the run and says so.

**Text is typed, or pasted when typing cannot work.** `pyautogui.write`
presses one key per character against the current layout, so it silently
drops any character that layout has no key for, and at one keystroke every
few milliseconds a paragraph gives an autocomplete popup time to eat half of
it. Over `COMPUTER_PASTE_THRESHOLD`, or with any non-ASCII in it, `_enter_text`
goes through the clipboard instead and puts the user's own clipboard back
afterwards. Typing stays the default for short plain strings: it is what
applications expect, and some fields refuse a paste outright. The handle
prototypes in `_clipboard_api` are not housekeeping - an undeclared
`GetClipboardData` returns a HANDLE truncated to 32 bits and the `GlobalLock`
on it takes the process down.

**Confirmation is per-run for `browser_task` and per-step for `screen_task`,
and that asymmetry is deliberate.** A DOM script is fully known before the
first step, so a purchase buried at step five is asked about at step zero. A
vision loop only discovers its next move by looking, so it asks when it gets
there. Saying yes re-runs `screen_task` with the same goal, which is safe
precisely because the loop is stateless: it re-reads the screen and carries on
from wherever things actually got to, rather than replaying what it already
did. Within one reply the whole batch is classified before any of it runs, on
the same argument as `browser_task`.

**Two classifiers, not one.** `tools/safety.py` gained `classify_gui`, which
reads the *description* of an action - the button label, the text about to be
typed, the goal of the run - because no regex over a command line will ever
notice that the button under the pointer says "Place order". Nothing in it is
BLOCKED; a GUI action has no equivalent of `format C:` that is never
legitimate, so the job is to ask rather than to refuse. Typed text goes
through `classify` *as well*, so a shell command typed into a focused terminal
meets the same blocked patterns it would have met through `terminal_command` -
arriving via the keyboard must not launder it. `classify_gui` is deliberately
not used on shell commands: its REVIEW list would flag every sentence
containing "move".

Screenshots stay in memory. The single path that writes one to disk is
`take_screenshot(save_as=...)`, and it resolves through
`file_manager.resolve_user_path`, so `FILE_ROOTS` governs it like any other
file E.V. writes.

`ev_core.SCREEN_TOOLS` gets a different acknowledgement from the generic
"stand by", spoken with no delay at all. The usual argument for waiting - that
a fast tool needs no announcement - does not apply to something that is about
to move the pointer under the user's hands.

**Vision waits out a rate limit the way the brain does.** `_post_json` reads
`retry-after` and sleeps once, up to `LLM_RATE_LIMIT_MAX_WAIT_S`. Vision needs
this more than chat does, not less: a screen task is a dozen requests in a row
against one per-minute budget, so it is both the likeliest thing to meet a 429
and the worst thing to lose to one - it meets it half way through, with real
work already done and the desktop left mid-job.

**`browser_task` keeps a profile, or it is permanently logged out.**
`launch_persistent_context` against `BROWSER_PROFILE_DIR` is what makes "open
Gmail and summarise the important mail" reach an inbox instead of a sign-in
page; Playwright's plain `launch` gives a blank browser with no cookies. The
profile is E.V.'s own rather than the user's real Chrome one, for two
reasons: Chrome locks its profile while it is running, so borrowing it would
fail whenever a browser was open, and automating a live signed-in profile is
a much bigger thing to hand a voice command. A locked or unwritable profile
costs the logins, not the errand - it falls back to a fresh browser with a
warning.

A `read` step returns **every** match, capped by `BROWSER_READ_ITEMS`, because
the interesting reads are lists: an inbox is thirty rows and a results page is
twenty cards. `inner_text` returns the first and nothing else, which is how a
summary of the important mail became a summary of one mail. What it reads goes
in `detail` and never in `speech` - raw page text is navigation labels,
timestamps and "1 of 47", and reading the first 180 characters of that aloud
was the worst available answer to "what's in my inbox". The model gets the
whole lot on the next turn, which is the compound-request machinery below
doing its job.

### Compound requests, and the token budget that shapes them

"Open my mail and give me a summary of the important things" is two jobs. The
model answers it with **one** tool call - it opens the mail, and the summary
never becomes a call at all. E.V. said "Opening your mail." and went quiet on
the only part the user was waiting for, which reads as being ignored. This is
not the `tool_use_failed` ladder: the call was accepted and correct, it was
just half the request.

`ev.session.split_followup` finds the trailing clause and `EV._run_followup`
takes one more turn for it. Three things keep that from being expensive or
wrong:

- **Only a trailing *question* qualifies.** An action followed by an action
  ("open Chrome and search for X") is one call on purpose, and re-running the
  tail of those would search twice. A question has no side effect to double,
  so a false positive costs one round trip while a false negative costs the
  user the answer.
- **Depth one, always.** A follow-up cannot spawn a follow-up; that is a loop
  with no ceiling, and the problem being solved is a request losing half of
  itself, not E.V. needing to plan.
- **Only after the first half actually finished.** A failed or cancelled
  first half stops it. A confirmation *holds* it in `_pending_followup`, so
  "tidy my desktop and tell me what moved" still answers after a spoken yes,
  and `_resolve_pending` claims it on every exit path so it cannot leak onto
  the next utterance.

`CHAIN_SETTLE_S` is not politeness. The first tool has usually just launched
something, and a screenshot taken before the window has drawn describes
whatever was there before it.

**The token budget is a real design constraint, not an implementation
detail.** Groq's free tier meters 8000 tokens a minute, and every request
carries the whole tool schema. At 13 tools that reached ~5046 tokens a
request, which left room for roughly one command per minute - so the second
turn of a compound request failed *by construction*. Trimming the schemas and
the tool rules brought the floor to ~3885, which is what makes two turns fit.
Keep new tool descriptions short for that reason: the cost is paid on every
utterance, including the ones that will never use the tool.

That floor is now ~3963 (~2876 of schema, ~1087 of prompt), and it is a
ceiling as much as a measurement: two turns must stay under 8000, so there
are about 35 tokens of slack. Teaching the model about `screen_task`'s new
verbs cost more than that in the first draft and had to be paid for by
compressing `mouse_action`'s field descriptions. Measure after any change
here - `len(json.dumps(to_openai_tools()))//4` plus the same for
`SYSTEM_PROMPT` - because going over does not fail loudly. It fails as the
second half of a compound request quietly not happening.

`Brain._post` also waits out a 429 once, using the delay named in the
response headers (`retry-after`, or `x-ratelimit-reset-tokens` as `2m52.8s` /
`547ms`) rather than a guessed backoff, and gives up if the window is longer
than `LLM_RATE_LIMIT_MAX_WAIT_S` - nobody stands at a microphone for a
minute. "Rate limited, give it a few seconds" is E.V. asking the user to do
waiting it could have done itself.

### Persistent state: memory and backlog

[ev/memory.py](ev/memory.py) and [ev/backlog.py](ev/backlog.py) are two small JSON files, because E.V. gets killed rather than closed — a lid, a Windows update, a power cut.

- **Writes are atomic.** Temp file in the same directory, `fsync`, then `os.replace` (atomic on Windows too). `ev.backlog` reuses `read_json`/`write_json` from `ev.memory` rather than duplicating that.
- **A corrupt file is a warning, not an error.** It is moved to `.corrupt` and E.V. starts empty. Losing preferences is survivable; refusing to boot is not.
- **The clean-shutdown flag is pessimistic.** Cleared and persisted at `begin_session`, set again only in `stop()`, so an unclean exit is detectable next time instead of looking like a tidy one.

`EV._boot()` is idempotent and called from both `start()` and `run_once()`, since `--say` reaches the loop by a different route. It assembles `Brain.session_context` — the offline gap, stored facts, open backlog — which rides in the system prompt under its own heading, separate from `extra_context` (which is about the last action). `EV._report_state()` speaks the welcome-back and the backlog summary in the interactive loop only.

A backlog entry is a reminder, **never a signed permission slip**: `Backlog.add` strips `confirmed` on the way in, and `backlog run` re-dispatches without it, so a delete declined on Monday is asked about again on Tuesday.

### Session modes and local intents

[ev/session.py](ev/session.py) defines `IDLE` (wake phrase required) / `ENGAGED` (name optional, decays after `CONVERSATION_WINDOW_S`) / `STANDBY` (ignores everything but "wake up"). Control phrases — "take five", "wake up", "stop", "goodbye" — are matched locally before the model is consulted, so they land instantly, including mid-sentence. Matching is **exact** against the normalised utterance (plus a leading "okay" or trailing "please"); fuzzy matching here would mistake "stop the server" for a cancel and drop a real request.

Wake detection in [ev/wake.py](ev/wake.py) *is* fuzzy, because STT mangles "E.V." into Eve, Evie, AV, heavy. It slices the command off the original transcript by character offset — slicing by word index desynchronises and eats the next word.

`is_resume_phrase()` is the one deliberate exception to the exact-match rule, and it runs **only in standby**. The risk that makes fuzzy matching wrong everywhere else — swallowing a real request — does not exist there, because standby has exactly two exits and accepts no commands. What it fixes is the opposite failure: "hey, wake up" falling through `match_intent` to a bare `return`, leaving the user with their words echoed, no reply, and no way back in. A non-matching utterance in standby now draws a hint (`ui.note`, never spoken — answering aloud would defeat standby), and anything longer than `STANDBY_MAX_UTTERANCE_S` is dropped before it costs a transcription call.

### Speech recognition

Whisper reports how sure it was, and E.V. used to throw that away by asking for `response_format=json`. The cost of ignoring it is not a wrong word on screen — it is a **wrong action**, because a garbled transcript is still handed to the model, which picks a tool and runs it. `verbose_json` costs nothing extra on the same free endpoint.

`Transcriber.transcribe` returns a `Transcript`, a `str` subclass carrying `avg_logprob`, `no_speech` and `compression`. The subclassing is what keeps the change small: every existing call site (`.lower()`, truthiness, `match_intent`, f-strings) works untouched. Note that `str` methods return plain `str`, so the metadata does not survive `.strip()` — read it before slicing, as `_tick` does. Three bands: `rejected` → "Didn't catch that", checked *after* `_extract_command` so an unaddressed utterance stays silent; `uncertain` → run it, but tell the model the words may be wrong; otherwise straight through. Backends that report no confidence (`google`, `whispercpp`) are `scored == False` and the gate leaves them alone.

The decoding prompt is built per-utterance by `Transcriber._prompt()` from hints the core loop supplies — installed programs, user folders, open backlog items — plus the previous transcript last, since Whisper reads the prompt as text immediately preceding the audio and weights the end most. `ev.stt` never reaches up into `tools`; `ev_core._stt_hints()` owns that wiring. The total is capped because Whisper silently drops the front of a prompt over ~224 tokens.

### Configuration

[config.py](config.py) holds every setting, each overridable by an `EV_*` environment variable, with `.env` loaded by a built-in parser (deferring to `python-dotenv` if installed). `.env.example` is the annotated template. Add new settings there rather than hardcoding them at the call site.

Note that `config.GROQ_MODEL` is mutated at runtime by `Brain.verify_model()`: Groq's catalogue varies per account, so a startup check falls back down `GROQ_MODEL_FALLBACKS` instead of erroring on every command.

`USER_DIRS` resolves Windows user folders through the `User Shell Folders` registry key rather than assuming `~/Desktop`, because OneDrive redirection means `~/Documents` and `~/OneDrive/Documents` can both exist while only the second is the one Explorer shows.

`STATE_DIR` (default `.cache/state`, git-ignored) holds `memory.json` and `backlog.json`. `MEMORY_FILE` and `BACKLOG_FILE` derive from it, so a test — or a second instance — can relocate all persisted state with one `EV_STATE_DIR`. `get_memory()` and `get_backlog()` rebuild their singleton when the configured path changes, which is what lets a test redirect the whole system with a single monkeypatch.

## Platform

Windows is the primary target. The core loop, brain, STT and TTS are portable; the `winmm` player, the App Paths registry lookup in [tools/base.py](tools/base.py), the Start Menu index in [tools/app_launcher.py](tools/app_launcher.py), `USER_DIRS`, and `dev_workflow`'s integrated-terminal path are Windows-specific and each has a documented fallback.

Nothing shells out to `cmd /c start` any more. For a name Windows cannot resolve, `start` pops a **modal error dialog and blocks** until it is dismissed — so an unknown app cost a ten-second freeze and an on-screen window before failing anyway.

`_shell_open` calls `ShellExecuteExW` through `ctypes` rather than `os.startfile`, for one reason: the `SEE_MASK_FLAG_NO_UI` flag. Without it the *shell* draws that dialog itself and the caller cannot stop it — `os.startfile` offers no way to ask for a silent failure. This matters most for Start Menu shortcuts, since a `.lnk` outlives the program it points at and every machine has a few aimed at things uninstalled months ago. Unknown apps now fail in ~0.15s with nothing on screen; `tests/test_standby_and_stt.py` asserts the `start` fallback has not crept back. Keep that pattern: guard with `IS_WINDOWS` and degrade rather than fail.
