"""`file_manager` - create, read, move, copy, rename, delete and organise files.

This is the tool with the most reach, so it is also the one with the most
rules. Three of them do the real work:

* **Everything resolves inside `config.FILE_ROOTS`.** A voice assistant acts on
  a transcript, and a transcript can be wrong. "Delete the temp folder" must
  not be able to resolve to `C:\\Windows\\Temp` because a word was misheard, so
  a path that escapes the roots is refused before anything touches the disk.
* **Destructive actions ask first.** Deleting and organising return
  `ToolResult.confirm`, which the core loop turns into a spoken yes/no. The
  model cannot skip this; `confirmed` is injected by the loop, never by the LLM.
* **Deletes are recoverable.** They go to the Recycle Bin when `send2trash` is
  installed, because "yes" said to the wrong question should not be final.

Spoken output names files the way a person would ("notes.txt in Documents"),
while the full path goes to `detail` for the model. Reading an absolute path
aloud character by character is unbearable.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import config
from tools.base import ToolResult

log = logging.getLogger("ev.tools.files")

# Spoken folder words that should resolve to a real user directory even when
# they arrive wrapped in filler: "my downloads folder", "the Desktop".
_DIR_NOISE = ("my ", "the ", "your ", "our ")
_DIR_SUFFIX = (" folder", " directory", " dir")

# Never writable, even inside the roots: editing these breaks the shell or
# hands over credentials.
_PROTECTED_NAMES = {
    "ntuser.dat",
    "ntuser.ini",
    ".ssh",
    ".aws",
    ".gnupg",
    "id_rsa",
    "id_ed25519",
    ".env",
}


class PathRefused(ValueError):
    """The path resolved outside the allowed roots, or onto a protected file."""


# -- path resolution ---------------------------------------------------------
def _normalise_dir_word(text: str) -> str:
    word = text.strip().strip("\"'").lower()
    for prefix in _DIR_NOISE:
        if word.startswith(prefix):
            word = word[len(prefix) :]
    for suffix in _DIR_SUFFIX:
        if word.endswith(suffix):
            word = word[: -len(suffix)]
    return word.strip()


def resolve_user_path(raw: str, default: Path | None = None) -> Path:
    """Turn whatever the model produced into an absolute path inside the roots.

    Accepts a spoken folder name ("Downloads"), a relative path
    ("Downloads/notes.txt"), `~`-relative paths, environment variables, and
    absolute paths. Raises `PathRefused` rather than silently retargeting,
    because quietly writing somewhere other than where the user asked is worse
    than refusing.
    """
    text = (raw or "").strip().strip("\"'")
    if not text:
        if default is None:
            raise PathRefused("no path given")
        return default

    text = os.path.expandvars(text)

    # "Documents", "my downloads folder" -> the real directory.
    word = _normalise_dir_word(text)
    if word in config.USER_DIRS:
        return _check(config.USER_DIRS[word])

    # "Downloads/notes.txt" -> anchor the leading word to the real directory.
    parts = text.replace("\\", "/").split("/")
    head = _normalise_dir_word(parts[0])
    if len(parts) > 1 and head in config.USER_DIRS:
        return _check(config.USER_DIRS[head].joinpath(*parts[1:]))

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        # A bare "notes.txt" belongs in the default folder, not in whatever
        # directory E.V. happens to have been started from.
        base = default if default is not None and default.is_dir() else config.FILE_DEFAULT_DIR
        candidate = base / candidate
    return _check(candidate)


def _check(path: Path) -> Path:
    """Resolve symlinks and confirm the result is inside an allowed root."""
    # `strict=False` so a file that does not exist yet still resolves - that is
    # the normal case for a create.
    resolved = Path(os.path.abspath(os.path.realpath(str(path))))

    if resolved.name.lower() in _PROTECTED_NAMES or any(
        part.lower() in _PROTECTED_NAMES for part in resolved.parts
    ):
        raise PathRefused(f"'{resolved.name}' is protected")

    for root in config.FILE_ROOTS:
        try:
            root_resolved = Path(os.path.abspath(os.path.realpath(str(root))))
        except OSError:
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved

    roots = ", ".join(str(r) for r in config.FILE_ROOTS)
    raise PathRefused(f"'{resolved}' is outside the allowed folders ({roots})")


def friendly(path: Path) -> str:
    """How a person would say this path out loud."""
    for name, directory in config.USER_DIRS.items():
        if path.parent == directory:
            return f"{path.name} in {name.title()}"
        if path == directory:
            return name.title()
    if path.parent == path.parent.parent:  # a drive root
        return path.name or str(path)
    return f"{path.name} in {path.parent.name}"


def _unique(path: Path) -> Path:
    """Never silently overwrite. `notes.txt` becomes `notes (2).txt`."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(2, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem} ({datetime.now():%Y%m%d%H%M%S}){suffix}"


def _size(path: Path) -> str:
    try:
        raw = path.stat().st_size
    except OSError:
        return "unknown size"
    for unit in ("bytes", "KB", "MB", "GB"):
        if raw < 1024 or unit == "GB":
            return f"{raw:.0f} {unit}" if unit == "bytes" else f"{raw:.1f} {unit}"
        raw /= 1024.0
    return f"{raw:.1f} GB"


def _is_directory_target(destination: Path, source: Path) -> bool:
    """Did the user mean "put it in here", or "call it this"?

    Getting this wrong is quietly destructive: "copy notes.txt to the Desktop"
    on a machine where the Desktop folder has not been created yet used to
    produce a *file* named `Desktop` in the home directory, with no error. So a
    named user folder always counts as a directory even when it does not exist
    yet, and so does any destination with no file extension when the source
    has one.
    """
    if destination.is_dir():
        return True
    if destination.exists():
        return False  # an existing file: the user named a replacement target
    if any(destination == known for known in config.USER_DIRS.values()):
        return True
    return bool(source.suffix) and not destination.suffix


# -- actions -----------------------------------------------------------------
def _create(path: Path, content: str, append: bool) -> ToolResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    if append and path.exists():
        with path.open("a", encoding="utf-8") as handle:
            handle.write(("" if content.endswith("\n") else "\n") + content)
        return ToolResult.success(
            f"Added to {friendly(path)}.", f"Appended {len(content)} chars to {path}"
        )

    target = path if not path.exists() else _unique(path)
    target.write_text(content, encoding="utf-8")
    lines = content.count("\n") + 1 if content else 0
    return ToolResult.success(
        f"Made {friendly(target)}.",
        f"Wrote {len(content)} chars ({lines} lines) to {target}",
    )


def _read(path: Path) -> ToolResult:
    if not path.exists():
        return ToolResult.failure(
            f"There's no {path.name} there.", f"Not found: {path}"
        )
    if path.is_dir():
        return _list(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult.failure("Couldn't open that one.", f"Read failed: {exc}")

    clipped = text[: config.FILE_MAX_READ_CHARS]
    truncated = " (truncated)" if len(text) > len(clipped) else ""
    # The model gets the contents; the user gets told it is open, because
    # reading a whole file aloud is almost never what was wanted.
    return ToolResult.success(
        f"Got {friendly(path)}. {len(text.splitlines())} lines.",
        f"Contents of {path}{truncated}:\n{clipped}",
    )


def _list(path: Path) -> ToolResult:
    if not path.is_dir():
        return ToolResult.failure(
            f"{path.name} isn't a folder.", f"Not a directory: {path}"
        )
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        return ToolResult.failure("Couldn't open that folder.", f"List failed: {exc}")

    folders = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]
    listing = "\n".join(
        f"  {'[dir] ' if p.is_dir() else ''}{p.name}" for p in entries[: config.FILE_MAX_BATCH]
    )
    if not entries:
        return ToolResult.success(f"{path.name} is empty.", f"{path} is empty")

    speech = (
        f"{len(files)} files and {len(folders)} folders in {path.name}."
        if folders
        else f"{len(files)} files in {path.name}."
    )
    return ToolResult.success(speech, f"Contents of {path}:\n{listing}")


def _copy(source: Path, destination: Path) -> ToolResult:
    if not source.exists():
        return ToolResult.failure(
            f"Can't find {source.name}.", f"Source not found: {source}"
        )
    # "copy x to Documents" names a folder, not a filename.
    if _is_directory_target(destination, source):
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination / source.name
    destination = _unique(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    except OSError as exc:
        return ToolResult.failure("That copy failed.", f"Copy failed: {exc}")
    return ToolResult.success(
        f"Copied {source.name} to {friendly(destination.parent)}.",
        f"Copied {source} -> {destination}",
    )


def _move(source: Path, destination: Path, renaming: bool) -> ToolResult:
    if not source.exists():
        return ToolResult.failure(
            f"Can't find {source.name}.", f"Source not found: {source}"
        )
    if not renaming and _is_directory_target(destination, source):
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination / source.name
    destination = _unique(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        return ToolResult.failure("That move failed.", f"Move failed: {exc}")

    if renaming:
        return ToolResult.success(
            f"Renamed it to {destination.name}.", f"Renamed {source} -> {destination}"
        )
    return ToolResult.success(
        f"Moved {source.name} to {friendly(destination.parent)}.",
        f"Moved {source} -> {destination}",
    )


def _send_to_trash(path: Path) -> bool:
    """Recycle Bin if we can, so a mistaken confirmation is recoverable."""
    if not config.FILE_USE_TRASH:
        return False
    try:
        from send2trash import send2trash
    except ImportError:
        return False
    try:
        send2trash(str(path))
        return True
    except Exception as exc:
        log.debug("send2trash failed for %s: %s", path, exc)
        return False


def _delete(path: Path) -> ToolResult:
    if not path.exists():
        return ToolResult.failure(
            f"There's no {path.name} to delete.", f"Not found: {path}"
        )

    label = friendly(path)
    if _send_to_trash(path):
        return ToolResult.success(
            f"{label} is in the Recycle Bin.", f"Recycled {path}"
        )
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        return ToolResult.failure("Couldn't delete that.", f"Delete failed: {exc}")
    return ToolResult.success(f"{label} is gone.", f"Permanently deleted {path}")


def _mkdir(path: Path) -> ToolResult:
    if path.exists():
        return ToolResult.success(
            f"{path.name} is already there.", f"Already exists: {path}"
        )
    try:
        path.mkdir(parents=True)
    except OSError as exc:
        return ToolResult.failure("Couldn't make that folder.", f"mkdir failed: {exc}")
    return ToolResult.success(f"Made the {path.name} folder.", f"Created {path}")


def _find(path: Path, pattern: str) -> ToolResult:
    if not path.is_dir():
        return ToolResult.failure(
            f"{path.name} isn't a folder.", f"Not a directory: {path}"
        )
    glob = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
    try:
        hits = [p for p in path.rglob(glob) if p.is_file()][: config.FILE_MAX_BATCH]
    except OSError as exc:
        return ToolResult.failure("That search failed.", f"Search failed: {exc}")

    if not hits:
        return ToolResult.failure(
            f"Nothing matching {pattern} in {path.name}.",
            f"No matches for '{glob}' under {path}",
        )
    listing = "\n".join(f"  {p}" for p in hits[:40])
    first = hits[0].name
    speech = (
        f"Found {first}."
        if len(hits) == 1
        else f"Found {len(hits)}. First one's {first}."
    )
    return ToolResult.success(speech, f"Matches for '{glob}' under {path}:\n{listing}")


def _category(suffix: str) -> str:
    lowered = suffix.lower()
    for name, extensions in config.FILE_CATEGORIES.items():
        if lowered in extensions:
            return name
    return "Other"


def _organize(path: Path) -> ToolResult:
    """Sort loose files in a folder into per-type subfolders."""
    if not path.is_dir():
        return ToolResult.failure(
            f"{path.name} isn't a folder.", f"Not a directory: {path}"
        )
    try:
        loose = [p for p in path.iterdir() if p.is_file()][: config.FILE_MAX_BATCH]
    except OSError as exc:
        return ToolResult.failure("Couldn't read that folder.", f"List failed: {exc}")

    if not loose:
        return ToolResult.success(
            f"{path.name} is already tidy.", f"No loose files in {path}"
        )

    moved: dict[str, int] = {}
    failures: list[str] = []
    for item in loose:
        bucket = _category(item.suffix)
        target_dir = path / bucket
        try:
            target_dir.mkdir(exist_ok=True)
            shutil.move(str(item), str(_unique(target_dir / item.name)))
        except OSError as exc:
            failures.append(f"{item.name}: {exc}")
            continue
        moved[bucket] = moved.get(bucket, 0) + 1

    if not moved:
        return ToolResult.failure(
            "Couldn't move anything.", "All moves failed:\n" + "\n".join(failures[:10])
        )
    total = sum(moved.values())
    summary = ", ".join(f"{count} to {name}" for name, count in sorted(moved.items()))
    detail = f"Organised {path}: {summary}."
    if failures:
        detail += f" {len(failures)} failed:\n" + "\n".join(failures[:10])
    return ToolResult.success(
        f"Sorted {total} files into {len(moved)} folders.", detail
    )


# -- dispatch ----------------------------------------------------------------
_NEEDS_CONFIRMATION = {"delete", "organize"}


def file_manager(
    action: str = "",
    path: str = "",
    destination: str = "",
    content: str = "",
    pattern: str = "",
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    """Single entry point for every file operation. Never raises."""
    verb = (action or "").strip().lower()
    if not verb:
        return ToolResult.failure("You didn't say what to do with it.")

    # Synonyms the model reaches for.
    verb = {
        "write": "create",
        "new": "create",
        "make": "create",
        "open": "read",
        "cat": "read",
        "ls": "list",
        "dir": "list",
        "remove": "delete",
        "rm": "delete",
        "trash": "delete",
        "search": "find",
        "organise": "organize",
        "tidy": "organize",
        "sort": "organize",
        "mkdir": "makedir",
        "folder": "makedir",
    }.get(verb, verb)

    try:
        target = resolve_user_path(path, default=config.FILE_DEFAULT_DIR)
        other = resolve_user_path(destination) if destination.strip() else None
    except PathRefused as exc:
        log.warning("Refused path: %s", exc)
        return ToolResult.failure(
            "That's outside the folders I'm allowed to touch.",
            f"Refused: {exc}. Do not retry; ask the user to widen EV_FILE_ROOTS.",
        )

    # Destructive actions are held for a spoken yes. `confirmed` only ever
    # arrives from the core loop, never from the model.
    if verb in _NEEDS_CONFIRMATION and config.FILE_CONFIRM_DELETE and not confirmed:
        what = "delete" if verb == "delete" else "reorganise"
        return ToolResult.confirm(
            f"That'll {what} {friendly(target)}. Sure?",
            f"Awaiting confirmation to {verb} {target}.",
            action=verb,
            path=str(target),
            destination=str(other) if other else "",
            content=content,
            pattern=pattern,
        )

    if verb == "create":
        return _create(target, content, append=False)
    if verb == "append":
        return _create(target, content, append=True)
    if verb == "read":
        return _read(target)
    if verb == "list":
        return _list(target)
    if verb == "makedir":
        return _mkdir(target)
    if verb == "delete":
        return _delete(target)
    if verb == "organize":
        return _organize(target)
    if verb == "find":
        return _find(target, pattern or content or "*")
    if verb in {"copy", "move", "rename"}:
        if other is None:
            return ToolResult.failure(
                "You didn't say where to put it.",
                f"'{verb}' needs a destination argument.",
            )
        if verb == "copy":
            return _copy(target, other)
        return _move(target, other, renaming=verb == "rename")

    return ToolResult.failure(
        "I don't know that file operation.",
        f"Unknown action '{action}'. Valid: create, append, read, list, copy, "
        "move, rename, delete, makedir, find, organize.",
    )
