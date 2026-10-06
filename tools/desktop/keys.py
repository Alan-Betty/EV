"""Key names, as pyautogui spells them, mapped to X keysyms and evdev codes.

The Wayland input backends speak two different languages. Mutter's remote
desktop session takes *keysyms* - "the character A" - and finds the key for
it in whatever layout is active, applying Shift itself. A uinput device
takes *evdev keycodes* - "the physical key in the A position" - so the
layout is ours to know, and this table assumes US for printable ASCII.
Anything a table cannot express goes through the clipboard instead, which is
the same rule `computer_use._needs_paste` already applies on Windows.

Names follow pyautogui, because that is the vocabulary `keyboard_action`
and the step planner already use: "enter", "ctrl", "win", "pagedown".
"""

from __future__ import annotations

# --- keysyms ---------------------------------------------------------------
_NAMED_KEYSYMS: dict[str, int] = {
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "esc": 0xFF1B,
    "escape": 0xFF1B, "backspace": 0xFF08, "delete": 0xFFFF, "del": 0xFFFF,
    "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55,
    "pgup": 0xFF55, "pagedown": 0xFF56, "pgdn": 0xFF56, "left": 0xFF51,
    "up": 0xFF52, "right": 0xFF53, "down": 0xFF54, "space": 0x20,
    "shift": 0xFFE1, "shiftleft": 0xFFE1, "shiftright": 0xFFE2,
    "ctrl": 0xFFE3, "ctrlleft": 0xFFE3, "ctrlright": 0xFFE4, "control": 0xFFE3,
    "alt": 0xFFE9, "altleft": 0xFFE9, "altright": 0xFFEA, "option": 0xFFE9,
    "win": 0xFFEB, "winleft": 0xFFEB, "winright": 0xFFEC, "super": 0xFFEB,
    "command": 0xFFEB, "cmd": 0xFFEB, "meta": 0xFFEB,
    "capslock": 0xFFE5, "printscreen": 0xFF61, "prtsc": 0xFF61,
    "apps": 0xFF67, "menu": 0xFF67, "pause": 0xFF13,
    "volumemute": 0x1008FF12, "volumedown": 0x1008FF11, "volumeup": 0x1008FF13,
    "playpause": 0x1008FF14, "nexttrack": 0x1008FF17, "prevtrack": 0x1008FF16,
}
for _n in range(1, 25):
    _NAMED_KEYSYMS[f"f{_n}"] = 0xFFBE + _n - 1


def keysym(name: str) -> int | None:
    """The keysym for a key name or a single character, or None."""
    if not name:
        return None
    lowered = name.lower()
    if lowered in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[lowered]
    if len(name) == 1:
        return char_keysym(name)
    return None


def char_keysym(char: str) -> int:
    """Latin-1 characters are their own keysym; the rest are 0x01000000 + code point."""
    code = ord(char)
    if char == "\n":
        return 0xFF0D
    if char == "\t":
        return 0xFF09
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 + code


# --- evdev keycodes (linux/input-event-codes.h) ---------------------------
KEY = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
    "9": 10, "0": 11, "-": 12, "=": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23,
    "o": 24, "p": 25, "[": 26, "]": 27, "enter": 28, "ctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37,
    "l": 38, ";": 39, "'": 40, "`": 41, "shift": 42, "\\": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    ",": 51, ".": 52, "/": 53, "shiftright": 54, "alt": 56, "space": 57,
    "capslock": 58, "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63,
    "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "ctrlright": 97, "altright": 100, "home": 102, "up": 103, "pageup": 104,
    "left": 105, "right": 106, "end": 107, "down": 108, "pagedown": 109,
    "insert": 110, "delete": 111, "volumemute": 113, "volumedown": 114,
    "volumeup": 115, "pause": 119, "win": 125, "winright": 126, "apps": 127,
    "printscreen": 99, "playpause": 164, "nexttrack": 163, "prevtrack": 165,
}
_KEY_ALIASES = {
    "return": "enter", "escape": "esc", "del": "delete", "pgup": "pageup",
    "pgdn": "pagedown", "control": "ctrl", "ctrlleft": "ctrl", "shiftleft": "shift",
    "altleft": "alt", "option": "alt", "winleft": "win", "super": "win",
    "command": "win", "cmd": "win", "meta": "win", "menu": "apps", "prtsc": "printscreen",
    " ": "space", "\n": "enter", "\t": "tab",
}

# US layout: the characters that need Shift, and the key they share.
_SHIFTED = dict(zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./"))


def evdev_key(name: str) -> int | None:
    lowered = name.lower() if len(name) > 1 else name
    lowered = _KEY_ALIASES.get(lowered, lowered)
    return KEY.get(lowered)


def evdev_char(char: str) -> tuple[int, bool] | None:
    """(keycode, needs_shift) for a printable ASCII character on a US layout."""
    if char.isascii() and char.isalpha():
        return KEY[char.lower()], char.isupper()
    if char in _SHIFTED:
        return KEY[_SHIFTED[char]], True
    code = evdev_key(char)
    return (code, False) if code is not None else None
