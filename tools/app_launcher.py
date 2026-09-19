"""`open_app` - launch desktop applications.

Finding a program on Windows is harder than it sounds, and getting it wrong is
expensive in two different ways.

**The old fallback was worse than failing.** When nothing resolved, this used
to run `cmd /c start "" <name>`. For a name Windows cannot find, `start` pops
a *modal error dialog* and blocks until it is dismissed - so an unknown app
cost a ten-second freeze and left a window on screen, and E.V. still ended up
saying it could not find it. Nothing here shells out to `start` any more:
`os.startfile` does the same job, returns immediately, and raises instead of
drawing a dialog.

**A hand-written alias list can never be long enough.** `APP_ALIASES` in
config covers the common names, but it will never know about whatever the user
installed last week. So the Start Menu is indexed instead: every `.lnk`
Windows itself lists, cached to disk and refreshed daily. Shortcuts are
launched as shortcuts - Windows resolves the target, the working directory and
the arguments, which is exactly the part that is tedious to reimplement.

Resolution order is narrowest-to-widest, so an explicit alias always wins over
a fuzzy shortcut match.
"""

from __future__ import annotations

import logging
import os
import random
import re
import shlex
import time
from difflib import get_close_matches
from pathlib import Path

import config
from ev.memory import read_json, write_json
from tools.base import IS_WINDOWS, ToolResult, popen_detached, resolve_executable

log = logging.getLogger("ev.tools.app")

# Windows "shell:AppsFolder" / URI targets that have no PATH executable.
_URI_PREFIXES = ("ms-settings:", "ms-clock:", "shell:", "http://", "https://")

# Filler that arrives attached to a spoken app name: "open the Spotify app".
_NAME_NOISE_PREFIX = ("the ", "my ", "a ")
_NAME_NOISE_SUFFIX = (" app", " application", " program", " please")

# Shortcut folders nobody means when they name an app.
_SKIP_SHORTCUT_WORDS = (
    "uninstall",
    "readme",
    "release notes",
    "documentation",
    "help",
    "website",
    "web site",
    "manual",
    "license",
)


def _normalise_name(app: str) -> str:
    """Strip spoken filler and punctuation from an app name."""
    key = " ".join((app or "").lower().split()).strip(".!?,")
    changed = True
    while changed:
        changed = False
        for prefix in _NAME_NOISE_PREFIX:
            if key.startswith(prefix):
                key, changed = key[len(prefix):].strip(), True
        for suffix in _NAME_NOISE_SUFFIX:
            if key.endswith(suffix):
                key, changed = key[: -len(suffix)].strip(), True
    return key


# -- Start Menu index --------------------------------------------------------
def _start_menu_dirs() -> list[Path]:
    if not IS_WINDOWS:
        return []
    roots = []
    for variable in ("APPDATA", "ProgramData"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [path for path in roots if path.is_dir()]


def _scan_start_menu() -> dict[str, str]:
    """Map every Start Menu shortcut name onto its `.lnk` path.

    Walking a few hundred files takes tens of milliseconds and happens once a
    day, which is a fair price for E.V. knowing about every program the user
    has actually installed rather than the two dozen in `APP_ALIASES`.
    """
    found: dict[str, str] = {}
    for root in _start_menu_dirs():
        try:
            shortcuts = list(root.rglob("*.lnk"))
        except OSError as exc:
            log.debug("Could not read %s: %s", root, exc)
            continue
        for shortcut in shortcuts:
            name = _normalise_name(shortcut.stem)
            if not name or any(word in name for word in _SKIP_SHORTCUT_WORDS):
                continue
            # First writer wins, so the per-user Start Menu takes precedence
            # over the machine-wide one.
            found.setdefault(name, str(shortcut))
    return found


class _AppIndex:
    """The Start Menu index, cached on disk and refreshed daily."""

    def __init__(self) -> None:
        self._apps: dict[str, str] | None = None
        self._last_scan = 0.0

    @property
    def path(self) -> Path:
        return config.STATE_DIR / "apps.json"

    def apps(self) -> dict[str, str]:
        if self._apps is None:
            self._apps = self._load()
        return self._apps

    def _load(self) -> dict[str, str]:
        if not IS_WINDOWS or not config.APP_INDEX_ENABLED:
            return {}
        cached = read_json(self.path, {})
        entries = cached.get("apps")
        scanned_at = float(cached.get("scanned_at") or 0.0)
        fresh = time.time() - scanned_at < config.APP_INDEX_TTL_S
        if isinstance(entries, dict) and entries and fresh:
            return {str(k): str(v) for k, v in entries.items()}
        return self.rescan()

    def rescan(self) -> dict[str, str]:
        if not IS_WINDOWS or not config.APP_INDEX_ENABLED:
            return {}
        apps = _scan_start_menu()
        self._apps = apps
        self._last_scan = time.monotonic()
        if apps:
            write_json(
                self.path,
                {"version": 1, "scanned_at": time.time(), "apps": apps},
            )
        log.info("Indexed %d Start Menu shortcuts", len(apps))
        return apps

    def find(self, name: str) -> tuple[str, str] | None:
        """Return (display name, .lnk path) for a spoken app name, or None.

        A miss triggers a rescan, rate-limited to one per cooldown, so a
        program installed during this session is findable without waiting out
        the daily TTL - and a name that genuinely does not exist cannot make
        every attempt walk the Start Menu again.
        """
        hit = self._match(self.apps(), name)
        if hit is None and time.monotonic() - self._last_scan > config.APP_INDEX_RESCAN_S:
            hit = self._match(self.rescan(), name)
        return hit

    @staticmethod
    def _match(apps: dict[str, str], name: str) -> tuple[str, str] | None:
        if not apps or not name:
            return None
        if name in apps:
            return name, apps[name]
        # "chrome" should find "google chrome"; prefer the shortest match, so
        # "code" does not land on "code - insiders".
        prefixed = sorted(
            (key for key in apps if key.startswith(name) or name in key.split()),
            key=len,
        )
        if prefixed:
            return prefixed[0], apps[prefixed[0]]
        near = get_close_matches(name, list(apps), n=1, cutoff=0.8)
        if near:
            return near[0], apps[near[0]]
        return None


_index = _AppIndex()


def app_index() -> _AppIndex:
    """The process-wide Start Menu index, shared with `--check`."""
    return _index


# -- launching ---------------------------------------------------------------
def _candidates(app: str) -> list[str]:
    """Map a spoken app name onto launch candidates, tolerating misheard words."""
    key = _normalise_name(app)
    if key in config.APP_ALIASES:
        return config.APP_ALIASES[key]

    near = get_close_matches(key, config.APP_ALIASES.keys(), n=1, cutoff=0.75)
    if near:
        return config.APP_ALIASES[near[0]]

    return [key.replace(" ", "")]


def _launched(name: str) -> str:
    """A short, varied confirmation. "Opening chrome." every time reads like a
    status log rather than someone talking."""
    return random.choice(
        (f"{name}'s up.", f"{name}, up.", f"Got it, {name}'s open.", f"There's {name}.")
    )


def _shell_open(target: str) -> bool:
    """Hand a URI or shortcut to the shell, with the error UI switched off.

    This calls `ShellExecuteExW` directly rather than `os.startfile`, for one
    reason: the `SEE_MASK_FLAG_NO_UI` flag. Without it the shell draws
    *"Windows cannot find ... Make sure you typed the name correctly"* itself,
    and there is nothing the caller can do about it - `os.startfile` has no way
    to ask for a silent failure. With it, a target that cannot be opened comes
    back as a return code and E.V. says so in its own words.

    That matters most for Start Menu shortcuts. A `.lnk` outlives the program
    it points at, so any machine has a few pointing at things that were
    uninstalled months ago, and launching one of those is precisely the case
    that used to put a dialog on screen.
    """
    if not IS_WINDOWS:
        return False

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - ctypes is always present on Windows
        return False

    class _ShellExecuteInfoW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_FLAG_NO_UI = 0x00000400
    SEE_MASK_NOASYNC = 0x00000100  # finish the call before we return
    SW_SHOWNORMAL = 1

    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_FLAG_NO_UI | SEE_MASK_NOASYNC
    info.lpVerb = "open"
    info.lpFile = target
    info.nShow = SW_SHOWNORMAL

    try:
        ok = bool(ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)))
    except (AttributeError, OSError) as exc:
        log.debug("ShellExecuteExW unavailable for %s: %s", target, exc)
        return False

    if not ok:
        log.debug(
            "Shell open failed for %s (error %s)", target, ctypes.GetLastError()
        )
    return ok


# An argument that is trying to be a filesystem location rather than a flag.
_PATHISH = re.compile(r"^(~|\.{1,2}[/\\]|[a-zA-Z]:[/\\]|[/\\]{1,2})")


def _looks_like_path(text: str) -> bool:
    """True for an argument the model meant as a folder or file.

    Switches are the thing to avoid mistaking for one. `-f`, `/select,` and
    `--profile=x` all contain characters a path contains; none of them are
    somewhere on disk, and path-checking them would refuse a perfectly good
    launch.
    """
    value = text.strip().strip('"')
    if not value or value.startswith(("-", "/")):
        return False
    return bool(_PATHISH.match(value)) or "\\" in value or "/" in value


def _repair_path(text: str) -> str | None:
    """Resolve a path argument, or None if there is no such place.

    The model will happily invent `C:\\Users\\Alan\\GitHub` for a machine whose
    user is not called Alan. Explorer opens its default location for a path
    that does not exist and exits 0, so without this the launch "succeeds",
    E.V. says so, and the user is looking at the wrong folder wondering why
    nothing happened.

    A wrong absolute path is usually right about the *last* part, so the
    basename is looked up the way a spoken folder name would be before giving
    up. Resolution goes through `file_manager`, which means the roots apply:
    an app cannot be used to open a folder that `file_manager` would refuse.
    """
    from tools.file_manager import PathRefused, resolve_user_path

    direct = Path(os.path.expandvars(text.strip().strip('"'))).expanduser()
    if direct.exists():
        return str(direct)

    # "C:/Users/Alan/GitHub" on a machine with no Alan: the tail is still the
    # folder they meant.
    for guess in (text, direct.name):
        if not guess:
            continue
        try:
            candidate = resolve_user_path(str(guess), default=config.FILE_DEFAULT_DIR)
        except (PathRefused, ValueError, OSError):
            continue
        if candidate.exists():
            log.info("Repaired app argument %r to %s", text, candidate)
            return str(candidate)
    return None


def open_app(app: str = "", arguments: str = "", **_: object) -> ToolResult:
    if not app.strip():
        return ToolResult.failure("You didn't say which app.")

    extra: list[str] = []
    if arguments and arguments.strip():
        try:
            extra = shlex.split(arguments, posix=not IS_WINDOWS)
        except ValueError:
            extra = [arguments.strip()]

        # Check any path argument before it is handed to a process. A program
        # launched at a location that is not there looks, from the outside,
        # exactly like one launched at a location that is.
        for index, item in enumerate(extra):
            if not _looks_like_path(item):
                continue
            repaired = _repair_path(item)
            if repaired is None:
                return ToolResult.failure(
                    f"I can't find that folder, so I've left {app.strip()} alone.",
                    f"'{item}' does not exist, so {app!r} was not launched - "
                    "opening it there would have silently landed somewhere "
                    "else. Never invent a path. Use file_manager with action "
                    "'open' and the folder as the user said it, or 'find' to "
                    "locate it first.",
                )
            extra[index] = repaired

    spoken = _normalise_name(app)
    pretty = (spoken or app.strip()).title()

    # 1. A configured alias, resolved to a real executable. Narrowest first,
    #    so an explicit mapping always beats a fuzzy shortcut match.
    for candidate in _candidates(app):
        if candidate.startswith(_URI_PREFIXES):
            if _shell_open(candidate):
                return ToolResult.success(_launched(pretty), f"Launched URI {candidate}")
            continue

        exe = resolve_executable(candidate)
        if exe:
            try:
                popen_detached([exe, *extra])
                return ToolResult.success(
                    _launched(pretty), f"Launched {exe} {' '.join(extra)}".strip()
                )
            except OSError as exc:
                log.debug("Direct launch failed for %s: %s", exe, exc)

    # 2. The Start Menu. This is what knows about everything installed since
    #    `APP_ALIASES` was last edited.
    hit = _index.find(spoken)
    if hit is not None:
        name, shortcut = hit
        if extra:
            # A shortcut carries its own arguments, so appending to it is not
            # possible through the shell. Resolve the target instead when the
            # user actually asked for arguments.
            target = resolve_executable(name.replace(" ", "")) or resolve_executable(name)
            if target:
                try:
                    popen_detached([target, *extra])
                    return ToolResult.success(
                        _launched(name.title()), f"Launched {target} {' '.join(extra)}"
                    )
                except OSError as exc:
                    log.debug("Launch with arguments failed for %s: %s", target, exc)
        if _shell_open(shortcut):
            return ToolResult.success(
                _launched(name.title()), f"Launched Start Menu shortcut {shortcut}"
            )

    # 3. The name exactly as said, in case it is on PATH but unaliased.
    exe = resolve_executable(spoken.replace(" ", "")) or resolve_executable(spoken)
    if exe:
        try:
            popen_detached([exe, *extra])
            return ToolResult.success(_launched(pretty), f"Launched {exe}")
        except OSError as exc:
            log.debug("Direct launch failed for %s: %s", exe, exc)

    # Fail fast and quietly. The old shell fallback turned this into a
    # ten-second hang and an error dialog for exactly the same outcome.
    return ToolResult.failure(
        f"Can't find {pretty} on this machine.",
        f"No executable or Start Menu shortcut resolved for '{app}'. "
        f"{len(_index.apps())} shortcuts indexed. Do not retry with a "
        "different spelling; ask the user what the program is called.",
    )
