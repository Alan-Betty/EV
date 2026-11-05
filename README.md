# L.E.O.

Latency-optimized Executive Operator. A voice assistant for Windows and Linux (Ubuntu, Hyprland) that does things on your computer: opens apps, moves files, runs commands, drives the browser, and can take over the mouse and keyboard to finish a task.

```
you > hey Leo, open Chrome and look for a good gaming mouse
L.E.O. > On it.
you > tidy up my downloads folder
L.E.O. > Forty-one files, seven folders. Downloads is civilised again.
you > take five
L.E.O. > Standing by.
```

Say "Leo" once and keep talking. The conversation stays open for a few seconds after you stop.

## Getting started

```bash
python -m pip install -r requirements.txt
cp .env.example .env          # add a free Groq or Gemini API key
python leo_core.py --check     # check the setup
python leo_core.py             # start talking
```

Keys: [Groq](https://console.groq.com/keys) (default) or [Google AI Studio](https://aistudio.google.com/apikey).

Other ways to run it:

```bash
python leo_core.py --text                 # type instead of talk
python leo_core.py --say "open notepad"   # one command, then exit
python -m pytest tests/ -q               # run the tests (offline)
```

Optional extras: `playwright` for browser tasks, `PySide6-Essentials` for the animated face, `send2trash` so deletes go to the bin, [`wacli`](https://github.com/openclaw/wacli) for WhatsApp, `kdeconnect-cli` to send files to your phone.

Upgrading from E.V.? Old `EV_*` settings still work, and `--check` lists any worth renaming to `LEO_*`.

## Things to try

- "open Spotify"
- "open my GitHub folder"
- "copy all the PDFs from downloads to documents"
- "what's on my screen"
- "find a wireless mouse on Amazon and add the cheapest one to my cart"
- "pause the music"
- "remember I prefer Firefox"
- "remember Priya is allergic to peanuts", then later "what should I cook for Priya"
- "what's using so much memory" or "spin up the dev server"
- "what did Priya say on WhatsApp"
- "start a coding session to fix the failing test"
- "stop everything" (or Ctrl+Alt+Q) to lock it down

Anything destructive, that spends money, or that sends a message asks "Confirm?" first.

## More

- [OVERVIEW.md](OVERVIEW.md): what Leo is, how it works, and what it can do
- [quickstart.md](quickstart.md): setup, tuning and troubleshooting
- [devlog.md](devlog.md): what changed recently
