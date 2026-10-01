"""`media_control` - the music that is already playing, and the volume knob.

"Pause the music", "next song", "turn it down", "what's playing" are the
commands a voice assistant hears most and the ones that most need to be
instant, so none of them goes anywhere near the screen. Every route here is
something the operating system already exposes, driven with no new package:

* **Linux** - players over MPRIS on the session bus, through `gdbus`, which
  every GNOME desktop has; the volume through `wpctl` (PipeWire), then
  `pactl` (PulseAudio), then `amixer` (ALSA). `playerctl` is used when it is
  there and never required.
* **Windows** - the media and volume keys, pressed through `keybd_event`.
  That is exactly what a keyboard's own media keys do, so whatever the
  system routes them to - Spotify, a browser tab, the volume flyout - answers
  as if the user had pressed them. Absolute volume is reached by stepping,
  because setting it directly needs COM and `pycaw`.
* **macOS** - the volume through `osascript`.

Not a side-effect tool for the runaway limiter, deliberately: "louder,
louder, louder" is three identical calls in a row, which is precisely the
pattern `tools.guard` reads as a loop and answers with lockdown. A volume
step is reversible by the next sentence, and that is not what the limiter
exists to stop.

One Linux trap is worth naming. A snapped browser (Ubuntu's Firefox, Brave)
is confined by AppArmor, and its MPRIS endpoint only answers unconfined
callers - so E.V. started from VS Code's terminal, itself a snap, is refused.
That arrives as `AccessDenied` and is reported as exactly that, rather than
as "nothing is playing".
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys

from tools.base import IS_WINDOWS, ToolResult

log = logging.getLogger("ev.tools.media")

IS_MAC = sys.platform == "darwin"
TIMEOUT_S = 3.0
DEFAULT_STEP = 10

_MPRIS = "org.mpris.MediaPlayer2"
_MPRIS_PATH = "/org/mpris/MediaPlayer2"
_PLAYER = "org.mpris.MediaPlayer2.Player"

_MEDIA_ACTIONS = {"play", "pause", "toggle", "next", "previous", "stop", "now_playing"}
_VOLUME_ACTIONS = {"volume_up", "volume_down", "set_volume", "mute", "unmute"}

# Spoken shorthand the model may pass through instead of the enum.
_ALIASES = {
    "play_pause": "toggle", "playpause": "toggle", "resume": "play",
    "skip": "next", "prev": "previous", "back": "previous",
    "status": "now_playing", "what": "now_playing",
    "louder": "volume_up", "up": "volume_up", "quieter": "volume_down",
    "down": "volume_down", "volume": "set_volume", "silence": "mute",
}


class MediaError(RuntimeError):
    """A media or volume command could not be carried out."""


def _run(argv: list[str]) -> str:
    """stdout of a command, or MediaError carrying its stderr."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except FileNotFoundError as exc:
        raise MediaError(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{argv[0]} did not answer") from exc
    if done.returncode != 0:
        raise MediaError((done.stderr or done.stdout or f"{argv[0]} failed").strip())
    return done.stdout


# -- Linux: players ------------------------------------------------------


def _gdbus(dest: str, method: str, *args: str, path: str = _MPRIS_PATH) -> str:
    return _run(["gdbus", "call", "--session", "--dest", dest,
                 "--object-path", path, "--method", method, *args])


def _players() -> list[str]:
    names = _gdbus("org.freedesktop.DBus", "org.freedesktop.DBus.ListNames",
                   path="/org/freedesktop/DBus")
    return re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", names)


def _prop(player: str, name: str) -> str:
    return _gdbus(player, "org.freedesktop.DBus.Properties.Get", _PLAYER, name)


def _status(player: str) -> str:
    """'Playing', 'Paused', 'Stopped' - or the refusal, which is worth keeping."""
    match = re.search(r"'(Playing|Paused|Stopped)'", _prop(player, "PlaybackStatus"))
    return match.group(1) if match else ""


def _pick_player() -> tuple[str, str]:
    """The player a bare "pause" means: the playing one, else a paused one."""
    players = _players()
    if not players:
        raise MediaError("no media player is running")
    statuses: dict[str, str] = {}
    refusal: MediaError | None = None
    for player in players:
        try:
            statuses[player] = _status(player)
        except MediaError as exc:
            statuses[player] = ""
            refusal = refusal or exc
    for wanted in ("Playing", "Paused"):
        for player, status in statuses.items():
            if status == wanted:
                return player, status
    # Every player refused to say: that is a sandbox, not an idle player,
    # and the next call would be refused in exactly the same way.
    if refusal is not None and not any(statuses.values()):
        raise refusal
    return players[0], statuses[players[0]]


def _pretty(player: str) -> str:
    """'org.mpris.MediaPlayer2.brave.instance116558' -> 'Brave'."""
    name = player[len(_MPRIS) + 1:].split(".")[0]
    return name.replace("_", " ").title() or "the player"


def _metadata(player: str) -> tuple[str, str]:
    raw = _prop(player, "Metadata")
    title = re.search(r"""'xesam:title': <(['"])(.*?)\1>""", raw)
    artist = re.search(r"""'xesam:artist': <\[(['"])(.*?)\1""", raw)
    return (title.group(2) if title else ""), (artist.group(2) if artist else "")


def _linux_media(action: str) -> ToolResult:
    if shutil.which("gdbus") is None and shutil.which("playerctl") is not None:
        return _playerctl(action)
    try:
        player, status = _pick_player()
    except MediaError as exc:
        return _media_failure(exc)
    who = _pretty(player)

    if action == "now_playing":
        try:
            title, artist = _metadata(player)
        except MediaError as exc:
            return _media_failure(exc)
        if not title:
            return ToolResult.success(f"{who} is {status.lower() or 'open'}, but it isn't saying what.",
                                      f"{player}: status={status}, no title in metadata")
        said = f"{title} by {artist}" if artist else title
        state = "" if status == "Playing" else f" ({status.lower()})"
        return ToolResult.success(f"{said}{state}.", f"{player}: {status} - {said}")

    method = {"play": "Play", "pause": "Pause", "toggle": "PlayPause",
              "next": "Next", "previous": "Previous", "stop": "Stop"}[action]
    try:
        _gdbus(player, f"{_PLAYER}.{method}")
    except MediaError as exc:
        return _media_failure(exc)
    return ToolResult.success(_media_speech(action, status), f"{method} sent to {player} (was {status})")


def _playerctl(action: str) -> ToolResult:
    verb = {"toggle": "play-pause", "now_playing": "metadata"}.get(action, action)
    argv = ["playerctl", verb]
    if action == "now_playing":
        argv += ["--format", "{{ title }} by {{ artist }}"]
    try:
        out = _run(argv).strip()
    except MediaError as exc:
        return _media_failure(exc)
    if action == "now_playing":
        return ToolResult.success(f"{out or 'Nothing I can name'}.", f"playerctl: {out}")
    return ToolResult.success(_media_speech(action, ""), f"playerctl {verb}")


# -- Linux: volume -------------------------------------------------------


def _linux_volume(action: str, level: int | None) -> int | None:
    """Apply a volume action; return the new level if the mixer reports one."""
    step = level if level is not None else DEFAULT_STEP
    if shutil.which("wpctl"):
        sink = "@DEFAULT_AUDIO_SINK@"
        if action == "set_volume":
            _run(["wpctl", "set-volume", "-l", "1.0", sink, f"{level}%"])
        elif action == "volume_up":
            _run(["wpctl", "set-volume", "-l", "1.0", sink, f"{step}%+"])
        elif action == "volume_down":
            _run(["wpctl", "set-volume", sink, f"{step}%-"])
        else:
            _run(["wpctl", "set-mute", sink, "1" if action == "mute" else "0"])
        match = re.search(r"Volume:\s*([\d.]+)", _run(["wpctl", "get-volume", sink]))
        return round(float(match.group(1)) * 100) if match else None
    if shutil.which("pactl"):
        sink = "@DEFAULT_SINK@"
        if action in ("mute", "unmute"):
            _run(["pactl", "set-sink-mute", sink, "1" if action == "mute" else "0"])
        else:
            value = {"set_volume": f"{level}%", "volume_up": f"+{step}%",
                     "volume_down": f"-{step}%"}[action]
            _run(["pactl", "set-sink-volume", sink, value])
        match = re.search(r"(\d+)%", _run(["pactl", "get-sink-volume", sink]))
        return int(match.group(1)) if match else None
    if shutil.which("amixer"):
        value = {"set_volume": f"{level}%", "volume_up": f"{step}%+", "volume_down": f"{step}%-",
                 "mute": "mute", "unmute": "unmute"}[action]
        out = _run(["amixer", "sset", "Master", value])
        match = re.search(r"\[(\d+)%\]", out)
        return int(match.group(1)) if match else None
    raise MediaError("no volume control found (wpctl, pactl or amixer)")


# -- Windows -------------------------------------------------------------

_VK = {
    "toggle": 0xB3, "play": 0xB3, "pause": 0xB3, "stop": 0xB2,
    "next": 0xB0, "previous": 0xB1,
    "mute": 0xAD, "unmute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
}
_KEYEVENTF_EXTENDEDKEY = 0x1
_KEYEVENTF_KEYUP = 0x2
# Each volume key press moves the Windows mixer by two points.
_WINDOWS_STEP = 2


def _press(vk: int, times: int = 1) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    for _ in range(max(1, times)):
        user32.keybd_event(vk, 0, _KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(vk, 0, _KEYEVENTF_EXTENDEDKEY | _KEYEVENTF_KEYUP, 0)


def _windows(action: str, level: int | None) -> ToolResult:
    if action == "now_playing":
        return ToolResult.failure(
            "I can't see what's playing on Windows.",
            "Track metadata needs the WinRT media session API, which E.V. does "
            "not use. take_screenshot can read it off the player instead.",
        )
    try:
        if action == "set_volume":
            # Down to the floor, then up to the level: the only way to an
            # absolute value with keys alone. Fifty presses is a few ms.
            _press(_VK["volume_down"], 100 // _WINDOWS_STEP)
            _press(_VK["volume_up"], round((level or 0) / _WINDOWS_STEP))
        elif action in ("volume_up", "volume_down"):
            step = level if level is not None else DEFAULT_STEP
            _press(_VK[action], max(1, round(step / _WINDOWS_STEP)))
        else:
            # Mute and unmute are one toggle key on Windows; it cannot be
            # asked which way it is, so both press it.
            _press(_VK[action])
    except (AttributeError, OSError) as exc:
        return ToolResult.failure("The media keys didn't go through.", f"keybd_event failed: {exc}")
    if action in _VOLUME_ACTIONS:
        return ToolResult.success(_volume_speech(action, level if action == "set_volume" else None),
                                  f"Pressed volume key for {action}")
    return ToolResult.success(_media_speech(action, ""), f"Pressed media key for {action}")


# -- macOS ---------------------------------------------------------------


def _mac_volume(action: str, level: int | None) -> int | None:
    current = int(_run(["osascript", "-e", "output volume of (get volume settings)"]).strip() or 0)
    step = level if level is not None else DEFAULT_STEP
    if action in ("mute", "unmute"):
        flag = "true" if action == "mute" else "false"
        _run(["osascript", "-e", f"set volume output muted {flag}"])
        return current
    target = {"set_volume": level or 0, "volume_up": current + step,
              "volume_down": current - step}[action]
    target = max(0, min(100, target))
    _run(["osascript", "-e", f"set volume output volume {target}"])
    return target


# -- speech --------------------------------------------------------------


def _media_speech(action: str, was: str) -> str:
    if action == "toggle":
        return "Paused." if was == "Playing" else "Playing."
    return {"play": "Playing.", "pause": "Paused.", "next": "Next one.",
            "previous": "Going back.", "stop": "Stopped."}.get(action, "Done.")


def _volume_speech(action: str, level: int | None) -> str:
    if action == "mute":
        return "Muted."
    if action == "unmute":
        return "Sound's back."
    if level is not None:
        return f"Volume's at {level}."
    return "Louder." if action == "volume_up" else "Quieter."


def _media_failure(exc: Exception) -> ToolResult:
    text = str(exc)
    if "no media player" in text:
        return ToolResult.failure(
            "Nothing's playing right now.",
            "No MPRIS media player is running. To start music, open a player "
            "with open_app or a site with browser_task - this tool only "
            "controls what is already playing.",
        )
    if "AccessDenied" in text or "AppArmor" in text:
        return ToolResult.failure(
            "The player won't take orders from me here - it's sandboxed.",
            "The player's MPRIS endpoint refused the call (AppArmor). This "
            "happens when E.V. runs inside a snap such as VS Code's terminal; "
            "run E.V. from a normal terminal. Do not retry.",
        )
    return ToolResult.failure("Couldn't reach the player.", f"Media control failed: {text[:300]}")


# -- the tool ------------------------------------------------------------


def media_control(action: str = "", level: int | None = None, **_: object) -> ToolResult:
    action = (action or "").strip().lower().replace(" ", "_").replace("-", "_")
    action = _ALIASES.get(action, action)
    if action not in _MEDIA_ACTIONS | _VOLUME_ACTIONS:
        return ToolResult.failure(
            "I'm not sure what to do with the music.",
            f"Unknown media action {action!r}. Valid: "
            f"{', '.join(sorted(_MEDIA_ACTIONS | _VOLUME_ACTIONS))}.",
        )
    if level is not None:
        try:
            level = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            level = None
    if action == "set_volume" and level is None:
        return ToolResult.failure("What volume? Say a number out of a hundred.",
                                  "set_volume needs level 0-100.")

    if IS_WINDOWS:
        return _windows(action, level)

    if action in _MEDIA_ACTIONS:
        if IS_MAC:
            return ToolResult.failure("I can only do the volume on a Mac for now.",
                                      "Media control is Linux and Windows only.")
        return _linux_media(action)

    try:
        now = _mac_volume(action, level) if IS_MAC else _linux_volume(action, level)
    except MediaError as exc:
        return ToolResult.failure("I couldn't change the volume.", f"Volume control failed: {exc}")
    shown = now if action not in ("mute", "unmute") else None
    return ToolResult.success(_volume_speech(action, shown),
                              f"{action}: volume now {now if now is not None else 'unknown'}%")
