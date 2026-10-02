# E.V.

A voice assistant for Windows and Ubuntu that actually does things on your computer: opens apps, moves files, runs commands, drives the browser, and can take over the mouse and keyboard to finish a task. It runs in around 120 MB of RAM because the model, speech recognition and voice all run in the cloud.

```
you  > hey EV, open Chrome and look for a good gaming mouse
E.V. > On it.
you  > tidy up my downloads folder
E.V. > Forty-one files, seven folders. Downloads is civilised again.
you  > take five
E.V. > Standing by.
```

Say "E.V." once and keep talking. The conversation stays open for a few seconds after you stop.

## Getting started

```bash
python -m pip install -r requirements.txt
cp .env.example .env          # add a free Groq or Gemini API key
python ev_core.py --check     # check the setup
python ev_core.py             # start talking
```

Keys: [Groq](https://console.groq.com/keys) (default) or [Google AI Studio](https://aistudio.google.com/apikey).

Other ways to run it:

```bash
python ev_core.py --text                 # type instead of talk
python ev_core.py --say "open notepad"   # one command, then exit
python -m pytest tests/ -q               # run the tests (offline)
```

Optional extras: `playwright` for browser tasks, `PySide6-Essentials` for the animated face, `send2trash` so deletes go to the bin.

## Things to try

- "open Spotify"
- "open my GitHub folder"
- "copy all the PDFs from downloads to documents"
- "what's on my screen"
- "find a wireless mouse on Amazon and add the cheapest one to my cart"
- "pause the music"
- "remember I prefer Firefox"
- "stop everything" (or Ctrl+Alt+Q) to lock it down

Anything destructive or that spends money asks "Confirm?" first.

## More

- [OVERVIEW.md](OVERVIEW.md): what E.V. is, how it works, and what it can do
- [quickstart.md](quickstart.md): setup, tuning and troubleshooting
- [devlog.md](devlog.md): what changed recently
