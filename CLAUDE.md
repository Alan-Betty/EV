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

python -m pytest tests/ -q                       # full suite, 317 tests, offline
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

### The speech-purity boundary

This is the most load-bearing invariant in the codebase, and it spans four files. E.V. once read "Spoke:" aloud, and the fix had two halves that must both stay intact:

- **Structural.** `Brain.remember(user, assistant, observation)` keeps the assistant role speech-only. Machine detail — exit codes, "Launched chrome.exe" — goes in `observation` and is replayed in an input role (a `system` message for Groq, a `user` turn for Gemini). Storing a tool observation as an assistant turn teaches the model, by example, to prefix its own replies with labels. `ToolResult.speech` is what gets said; `ToolResult.detail` is what the model sees next turn.
- **Boundary.** `clean_for_speech` in [ev/tts.py](ev/tts.py) strips label-shaped prefixes regardless of origin, then strips again after markdown removal (`**E.V.:** hi` hides a label behind markdown). It must not become aggressive enough to eat real speech like "Spoke to your mother" — `tests/test_speech_purity.py` pins both the strip-cases and the keep-cases.

[ev/ui.py](ev/ui.py) is a strict dead end supporting this: every function returns `None` and only draws. Labels, panels and spinners live there and nowhere else, and the reply text reaches `Speaker.say` by a separate path in `ev_core`. Never have a UI function return a string that flows toward the speaker.

### Tool contract

[tools/schemas.py](tools/schemas.py) is the single source of truth. `TOOL_SPECS` is provider-neutral JSON Schema; `to_openai_tools()` and `to_gemini_tools()` translate it, so a tool is described exactly once. Gemini rejects schema keys outside its OpenAPI subset — `test_smoke.py` asserts this.

`dispatch` in [tools/\_\_init\_\_.py](tools/__init__.py) is the only entry point. It filters arguments down to `_ALLOWED_ARGS`, which is derived from `TOOL_SPECS` so the two cannot drift, coerces the loose types LLMs emit, and never raises — a broken tool returns a failure `ToolResult` rather than killing the assistant.

Adding a tool means: a spec in `TOOL_SPECS`, an implementation returning `ToolResult`, and an entry in `REGISTRY`. Nothing else, and nothing outside the schema will reach the function.

### Confirmation and safety

`confirmed` is injected by the core loop after a spoken yes — it is added to `_ALLOWED_ARGS` manually and is never something the model can set for itself. The flow: a tool returns `ToolResult.confirm(...)` → `ev_core._execute` stores `session.pending` (holding the tool name too, since file deletes and shell commands both arrive here) → the next utterance goes to `_resolve_pending`. Only an unambiguous yes runs it; an utterance that is neither yes nor no is treated as the user moving on, and is re-dispatched as a fresh command rather than swallowed.

Two independent gates enforce this:

- [tools/safety.py](tools/safety.py) `classify()` returns `SAFE` / `REVIEW` / `BLOCKED`. Blocked patterns (disk format, pipe-to-shell, shadow-copy deletion, fork bombs) run under no confirmation at all.
- [tools/file_manager.py](tools/file_manager.py) `_check()` resolves every path through `realpath` and refuses anything outside `config.FILE_ROOTS`, raising `PathRefused`. A refused path is never silently retargeted. `tests/test_file_manager.py` and `tests/test_file_batch.py` monkeypatch `FILE_ROOTS` and `USER_DIRS` at a temp tree, so the suite can never touch a real user directory.

The batch actions (`batch_copy`, `batch_move`, `batch_rename`) act on a whole folder at once, so a leaked root would leak by the hundred: every individual source *and* destination goes back through `_check`, not just the two folders named in the call. They glob rather than `rglob` — "the PDFs in Downloads" is not "every PDF under my home folder" — and `batch_move`/`batch_rename` are gated like `delete`. `backlog clear` is gated the same way, which is why `backlog` is in the manual `confirmed` allow-list in `tools/__init__.py`.

`dev_workflow` is gated the same way: it only types into VS Code's integrated terminal once [tools/window.py](tools/window.py) confirms that window holds focus, and otherwise degrades to spawning a separate terminal rather than typing into whatever is on screen.

### Streaming

With `LLM_STREAMING` on and audio enabled, `Brain._decide_groq_streamed` pulls the `chat` reply out of half-written tool-call JSON via `partial_reply()` (hand-rolled, because `json.loads` is useless mid-stream) and hands complete sentences to a `SpeechStream`. **Only `chat` streams** — every other tool has a side effect, and announcing "Chrome's up" before Chrome is up would be a lie. The stream is created lazily on the first sentence and must be `cancel()`ed if the call turns out not to be `chat`, or it holds the speaker lock forever. Streaming is an optimisation and never a dependency: any failure falls back to a plain call.

### Immediate acknowledgement

A tool that launches an app or walks a folder tree takes seconds, and silence for those seconds reads as "it didn't hear me" — so the user repeats themselves and now there are two commands in flight. `EV._start_ack` draws an acknowledgement immediately and speaks one from a **background task**, so the dispatch is already running on its own thread before a syllable comes out. Awaiting the speech before the tool would make every command a second slower and defeat the whole thing.

It is also delayed by `ACK_DELAY_S`: a tool that returns in 200ms needs no "stand by", and `_finish_ack` cancels the task before it speaks. Once it *is* speaking it is allowed to finish — cancelling an `asyncio.to_thread` playback does not stop the OS thread, so cutting in would put E.V. on top of itself. `chat` is never acknowledged; it has no side effect to wait on and is already streaming its real reply.

### Cancelling a running tool

"Stop" used to do nothing once work was underway. The tool sits on a worker thread, the loop is blocked awaiting it, and a thread cannot be killed from outside — so cancellation is **cooperative**: `CancelToken` in [tools/base.py](tools/base.py) is a request the tool honours at a point where stopping is *safe*, between two files or two polls of a subprocess, never mid-write. That restraint is the design, not a limitation of it.

`dispatch(name, arguments, cancel)` attaches the token **after** argument filtering and type coercion — so the model can neither supply one nor clear one, and it never gets stringified on the way through. Only tools in `CANCELLABLE` receive it. `terminal_command` polls `Popen` instead of calling `subprocess.run` (which is precisely why "stop" was inert: `run` blocks the thread with no moment to act on anything) and drains stdout on reader threads, because a full pipe deadlocks a process nobody is reading from. `file_manager` checks between files in the batch actions and `organize`.

While a cancellable tool runs, `EV._watch_for_cancel` keeps the microphone open. Two rules there: an utterance that is **not** a cancel is held in `_queued_utterance` and handled by `_route` afterwards rather than discarded — talking over a slow tool is usually the next command — and a cancel aimed at a tool outside `CANCELLABLE` sets `_cancel_refused`, which E.V. says out loud. `open_app` has already launched the program; reporting a stop that did not happen would be worse than admitting it cannot.

A cancelled tool returns `ToolResult.stopped`, which is `ok=True` (the files that moved really did move) carrying `cancelled` in its data. The core loop reads that and backlogs the remainder as `interrupted`.

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
