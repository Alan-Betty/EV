# L.E.O. Roadmap

Ideas for later, grouped into phases you can ask for by letter: "implement phase B", or "do B1 and B2". Nothing here is built yet unless it says so. Each phase gets its own short plan before any code.

| Phase | What you get | Size | Needs first |
|---|---|---|---|
| [A](#a-prove-what-is-already-built) | Proof that what exists works on real hardware | S | Nothing |
| [B](#b-desktop-app-for-windows-macos-and-linux) | An installable desktop app on all three systems | XL | A |
| [C](#c-leos-windows-on-linux-done-properly) | Face, captions and overlay that belong on Linux | M | A |
| [D](#d-memory-you-can-see-and-trust) | A memory you can browse, correct and rely on | M | A |
| [E](#e-messages-beyond-reading-whatsapp) | Inbox triage, reply help, more channels | L | A |
| [F](#f-leo-speaks-first) | Reminders, briefings and alerts without being asked | M | D helps |
| [G](#g-a-real-coding-partner) | Fix-until-green loops, voice review, editor bridge | L | A |
| [H](#h-minecraft) | Server control, then an in-game helper | M | Nothing |
| [I](#i-make-the-name-true) | A faster voice loop, measured stage by stage | M | A |
| [J](#j-extensions) | New tools without touching the core | L | B1 helps |
| [K](#k-keep-it-healthy) | CI, soak tests and an injection test suite | M | Nothing |

Suggested order: **A, C, K, B, then whichever you want most.** A comes first because everything else builds on parts never run for real. C is the quick visible win. K before B, because three operating systems without CI will break quietly.

---

## A. Prove what is already built

These were left open at the end of the revamp. Most need you for a minute.

- **A1. Real turns.** Run voice and text with your key. Check `x-ratelimit-remaining-tokens` against the budget, which so far is only a `chars / 4` estimate.
- **A2. The kill key.** Press Ctrl+Alt+Q for real, with a native window focused, during a `screen_task`.
- **A3. The wake word.** Record "Leo" a few dozen times, run the clips through Whisper, and tune `leo/wake.py` from what it actually hears. Count false wakes on an hour of ordinary speech.
- **A4. WhatsApp for real.** Install and pair `wacli`, compare its JSON with `_field` in `tools/whatsapp.py`, and send one message to yourself or a spare number. Check whether it pairs with WhatsApp Business.
- **A5. Clicks and overlay on Hyprland.** Real clicks in a native app, and the red frame during a takeover.
- **A6. Phone transfer.** Pair KDE Connect and send a file.

**Done when:** each item is passed or has a filed bug, and `CLAUDE.md` no longer lists it as unproven.

## B. Desktop app for Windows, macOS and Linux

Today L.E.O. is a Python script you start from a terminal. This makes it something you install and forget about.

**Recommended shape** (confirm at the start of the phase, since it is hard to change later): a small [Tauri](https://tauri.app) shell for the tray, settings and updates, with the Python core bundled as a background process and the Qt face kept as its own process.

Why: Tauri's native webview stays light, which respects the memory limit this project is built around, and the settings UI can reuse the look of `site/`. The main alternative is a PySide6 app: one language and no Rust, but a heavier install and Qt settings screens. Rewriting the core is not worth considering; it holds too many hard-won fixes.

- **B1. Headless core.** `leo_core.py --serve`: the core runs with no terminal and talks JSON lines over a local socket, the same pattern as `FaceLink`. Everything after this depends on it, and it is useful on its own.
- **B2. Tray app.** Status dot, mute, standby, lockdown and quit. Lockdown stays reachable from the tray at all times.
- **B3. First-run setup.** Paste a key, pick a microphone, say "Leo" to test it, grant permissions. It replaces `.env` editing for normal users.
- **B4. Settings.** The useful 20 of the ~300 settings, with no helper text the layout could make unnecessary.
- **B5. Keys in the OS keychain**, not in a plain `.env`.
- **B6. Packaging.** Bundle Python with PyInstaller or Nuitka. Build an MSI or NSIS installer, a DMG, an AppImage, and an AUR package for you.
- **B7. Start at login and auto-update.** Updates are signed and always ask first.
- **B8. macOS parity.** The weakest platform today: microphone and accessibility permission prompts, audio playback, and real testing of `tools/desktop/macos.py`. Needs a Mac to test on.
- **B9. Signing.** Without it, Windows SmartScreen and macOS Gatekeeper warn or block. Apple needs a paid developer account, and Windows signing also costs money. Decide this before a public release.
- **B10. Point the site's download buttons at real builds.**

**Watch out for:** the tool list and `FILE_ROOTS` must keep working once paths move inside a bundle; Playwright's browser is large, so download it on first use; `dev_session` and `wacli` stay external tools, never bundled.

**Done when:** a fresh machine on each OS goes from download to a spoken reply with no terminal.

## C. Leo's windows on Linux, done properly

The tiling bug is fixed: the face and caption now float through XWayland on Hyprland. This phase goes further.

- **C1. Native layer-shell windows.** Move the face and caption to `wlr-layer-shell` on Hyprland and sway, so they stop needing XWayland. Keep XWayland as the fallback.
- **C2. Busy detection on Hyprland.** `leo/face/busy.py` only understands Windows and GNOME. Use `hyprctl` so the face hides during full-screen video and games.
- **C3. Eyes that follow the pointer.** XWayland only sees the pointer over X11 windows. Read `hyprctl cursorpos` instead.
- **C4. The right monitor.** Appear on the focused monitor, and follow it.
- **C5. A redesigned window.** Try a compact pill or an edge-docked face, a size setting, and a caption that shrinks to fit. Compare mockups first.
- **C6. Port the overlay off Tk.** Tk is X11-only and the one window not sharing the face's rendering. Move it to Qt, which also means less XWayland.
- **C7. Other compositors.** niri and KDE window tiers, following `tools/desktop/hyprland.py`. Ryoku also ships a niri edition.

**Done when:** on Hyprland, nothing of Leo's tiles, steals focus, or sits on top of a full-screen game.

## D. Memory you can see and trust

- **D1. Memory browser.** A local page showing the tree, each person's notes and their links, with edit and delete. It fits inside the B4 settings.
- **D2. Turn on learning safely.** Try `LEO_MEMORY_EXTRACT_ENABLED` as "suggest, then you approve" at first, so wrong notes never land silently.
- **D3. Sources.** Each note records when and how it was learned, so "how do you know that?" has an answer.
- **D4. Time-aware facts.** "Priya is in Pune until March" expires instead of staying true forever. Contradictions ask which is right.
- **D5. Seed people from chats.** Offer contacts you talk to most as branches, with your approval, and never message text without asking.
- **D6. Reinforced recall.** Notes you actually use rank higher, and a monthly "still true?" review handles the rest.
- **D7. Encrypted at rest and exportable.** It holds details about other people, so offer encryption, plain JSON export and a one-step wipe.

## E. Messages beyond reading WhatsApp

Your rule stands: **every send needs your yes.** Anything loosening that is a separate decision for you, and is marked below.

- **E1. Catch-up.** "What did I miss?" gives a short summary by person, most important first.
- **E2. Reply suggestions.** "Reply to Priya" reads the thread, drafts in your style and asks.
- **E3. Scheduled sends.** "Text Sam at 9." Asks now, and again if the wait was long or the chat has changed.
- **E4. Files from chats.** "Get the PDF Priya sent yesterday and put it on my phone." The parts exist; this joins them up.
- **E5. Style tuning.** Corrections to drafts feed the style profile, per contact, since you don't text your mum like your boss.
- **E6. Official channels first.** Telegram's bot API, and Gmail and Calendar through their own APIs. These carry no ban risk, unlike `wacli`.
- **E7. WhatsApp's official API.** Research only. The Business Cloud API is the sanctioned route, but check what it would mean for your number first.
- **E8. Your decision: auto-reply for a named safe list.** Declined so far. Higher ban risk, and those contacts could not tell it isn't you unless you say so.

## F. Leo speaks first

- **F1. Reminders with a time.** "Remind me at 5 to call the dentist", surviving restarts.
- **F2. Morning briefing.** To-do list, calendar, unread summary and unfinished backlog, on request or at a set time.
- **F3. Watches.** "Tell me if the dev server dies", "tell me when this download finishes". Uses the notifier that coding sessions already use.
- **F4. Quiet hours and busy rules.** It never interrupts a call, full-screen app or standby. Non-urgent things wait.
- **F5. Notification or voice.** Desktop notifications when speaking would be rude.
- **F6. A hard limit.** A daily cap on unprompted speech. A chatty assistant gets switched off.

## G. A real coding partner

- **G1. Fix until green.** A session may retry after failed tests, up to a set number of rounds and budget.
- **G2. Voice review.** "What did it change?" gives a spoken summary, and "show me" opens the diff.
- **G3. Merge and PR with your yes.** Sessions can't push today. This adds a confirmed step: a spoken summary, then Leo pushes or opens a PR.
- **G4. VS Code bridge.** Check whether the extension can open a session run from the CLI. If not, write a small extension showing live status.
- **G5. "Fix this."** Reads the error on screen or in the terminal and starts a session from it. Uses awareness and `dev_session`.
- **G6. Several at once.** Parallel sessions per repo, each named and with its own budget.
- **G7. A sandbox.** Run session tests in a container or bubblewrap. Today project tests run with your full user rights, which is the biggest open risk in this feature.
- **G8. Worktree cleanup.** "Throw that away" removes the branch and worktree, with a yes.

## H. Minecraft

Deferred by choice. Same adapter pattern as `wacli`: a thin subprocess wrapper, an on-demand tool and confirmation gates.

- **H1. Server control.** Start and stop through `system_control`, RCON commands, log reading, backups. No game mods, no new core dependency. Start here.
- **H2. In-game helper bot.** A Mineflayer bot, as a Node sidecar, that follows, mines and builds on command. Your own worlds, or ones whose rules allow bots.
- **H3. Fabric mod.** In-game chat with Leo and live world data. The deepest link, but it can break on every Minecraft update.

## I. Make the name true

"Latency-optimized" should be a measured claim. Today's numbers are in `CLAUDE.md`.

- **I1. Latency dashboard.** `LEO_TURN_TIMING` data over real sessions: median and worst per stage.
- **I2. Set a target.** For example, first sound under a set number of seconds, then treat a regression as a bug.
- **I3. Speculative transcription.** Start Whisper before the silence timer ends, and discard it if the speaker carries on.
- **I4. Faster voice.** Check newer low-latency TTS options, and cache common replies more widely.
- **I5. Optional local models.** whisper.cpp already works for speech. Add an Ollama brain for privacy and offline use, as an opt-in, so the 90 to 160 MB core stays small by default.
- **I6. Local wake word, opt-in.** So idle speech stops going to the cloud at all. Today it is still transcribed remotely, which is the biggest privacy gap in the voice loop. It costs memory, so it stays a choice.

## J. Extensions

- **J1. MCP client.** Use MCP servers as on-demand tools. Each tool's output counts as untrusted, and each stays behind the existing gates.
- **J2. Local plugin folder.** A file describes a tool's schema, triggers and risk level.
- **J3. Routines.** "When I say goodnight, stop the servers and mute." Saved sequences, each step still gated.
- **J4. More model providers.** Claude, OpenAI and local models behind the same `TOOL_SPECS` translation. This also removes the free-tier squeeze on the token budget for anyone with a paid key.

**Watch out for:** a plugin is code running as you. It needs a permission model before anyone else's plugin is installed.

## K. Keep it healthy

- **K1. CI.** There is none. Run the offline suite on Windows, macOS and Linux for each push, which is a must before phase B.
- **K2. Fix the offscreen face test.** `test_captions_follow_the_face_and_never_take_input` fails on Qt's fake 800x800 screen, since before the revamp.
- **K3. Prompt-injection suite.** A collection of hostile pages and messages, run against a real model, scoring what it tries to do. The fence and gates are tested; how often the model is actually fooled is not.
- **K4. Soak test.** Hours of running, watching memory and leftover processes.
- **K5. Tool-routing eval.** A set of real phrases and the tool each should reach. Run it before any change to triggers or prompts, because a miss in the on-demand tier gets no second chance.
- **K6. Rename the repo.** `Alan-Betty/EV` to something matching L.E.O., and update the site's links. GitHub redirects the old URL.
- **K7. Remove old names.** Drop `EV_*` fallback and legacy extension support after a release or two.
- **K8. Split `CLAUDE.md`.** It is over 100 KB and growing; move each subsystem's reasoning next to its code.
