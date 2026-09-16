"""List available Edge neural voices, so you can pick one for EV_TTS_VOICE.

    python -m ev.tts_voices           # English voices
    python -m ev.tts_voices en-GB     # filter by locale prefix
    python -m ev.tts_voices --all     # every voice, all languages
    python -m ev.tts_voices --demo en-US-GuyNeural   # hear one
"""

from __future__ import annotations

import asyncio
import sys


async def _list(prefix: str) -> int:
    import edge_tts

    voices = await edge_tts.list_voices()
    rows = [
        (voice["ShortName"], voice.get("Gender", "?"),
         ", ".join(voice.get("VoiceTag", {}).get("VoicePersonalities", [])) or "-")
        for voice in voices
        if voice["ShortName"].lower().startswith(prefix.lower())
    ]
    if not rows:
        print(f"No voices matching {prefix!r}.")
        return 1

    width = max(len(name) for name, _, _ in rows)
    for name, gender, personality in sorted(rows):
        print(f"  {name:<{width}}  {gender:<6}  {personality}")
    print(f"\n{len(rows)} voice(s). Set one as EV_TTS_VOICE in your .env file.")
    return 0


async def _demo(voice: str) -> int:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    import config

    config.TTS_VOICE = voice
    from ev.tts import Speaker

    speaker = Speaker()
    await speaker.warmup()
    if not speaker.enabled:
        print("No playback backend available.")
        return 1
    await speaker.say(f"This is {voice.split('-')[-1].replace('Neural', '')}. Ready when you are.")
    return 0


def main() -> int:
    args = [arg for arg in sys.argv[1:]]
    if args and args[0] == "--demo":
        if len(args) < 2:
            print("Usage: python -m ev.tts_voices --demo <VoiceShortName>")
            return 2
        return asyncio.run(_demo(args[1]))

    prefix = "" if args and args[0] == "--all" else (args[0] if args else "en-")
    return asyncio.run(_list(prefix))


if __name__ == "__main__":
    sys.exit(main())
