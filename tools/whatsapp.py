"""WhatsApp through `wacli`, a third-party linked-device client.

Unofficial, so automated use can break WhatsApp's terms and can cost the number.
Nothing here can rule that out. What it can do is behave nothing like a bot, and
the limits below are chosen for that reason: my judgement, since WhatsApp does
not publish what is safe.

Sending has exactly one road, and it asks first every single time:

1. The model passes what the user wants said. The planner rewrites it in the
   user's own style, from their own sent messages. Nothing anyone else wrote is
   in that prompt, so an incoming message cannot steer the draft.
2. The contact is resolved to one known person with an existing chat. A cold
   message is the likeliest way to be reported, so a number with no history is
   refused; so are groups and channels, in this version.
3. `ToolResult.confirm` reads the exact text aloud. `tools.from_model` strips
   `confirmed`, `jid` and `draft`, so the model cannot approve or retarget it.
4. The replay sends the stored text, not a fresh draft, after every limit is
   checked again. The attempt is written down *before* the send: wacli can drop
   the connection after the frame left, so a failure may still have been
   delivered. It never retries. A logged-out or banned answer, or repeated
   errors, latches sending off until `--whatsapp-reset`, a person's decision.

`tools.safety` blocks `wacli send` and friends for `terminal_command`, so the
shell cannot reach round all of this. Reads pass `--read-only` as well, so even
a mistake in this file cannot change anything through one.

Message text only ever goes in `detail`, never `speech`: speech is replayed as
the assistant's own words, unfenced, and anyone at all can message this number.

JSON field names come from wacli's docs and were not checked against a real
binary when this was written, hence the tolerant `_field`.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import config
from leo.memory import read_json, write_json
from tools.base import ToolResult

log = logging.getLogger("leo.tools.whatsapp")

_ALLOWED_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("messages", "search"),
        ("messages", "list"),
        ("chats", "list"),
        ("contacts", "search"),
        ("media", "download"),
        ("doctor",),
    }
)
_SEND_COMMAND = ("send", "text")
_LATCH_WORDS = re.compile(
    r"logged.?out|session.?revoked|not authenticated|not authori[sz]ed|\bbanned\b|"
    r"temporar\w* ban|account.*(blocked|suspended|restricted)",
    re.IGNORECASE,
)
_NOT_A_PERSON = ("@g.us", "@newsletter", "@broadcast")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_lock = threading.Lock()

_DRAFT_SYSTEM = """You rewrite what a user wants to say into the WhatsApp message \
they would type themselves, matching their style notes and examples: length, \
casing, punctuation, emoji, language. Say only what they asked to say. Never add \
facts, promises, greetings they did not imply, or anything from the examples. \
Reply as JSON: {"text": "the message"}"""


class WacliError(RuntimeError):
    def __init__(self, message: str, latch: bool = False, uncertain: bool = False) -> None:
        super().__init__(message)
        self.latch = latch
        self.uncertain = uncertain


# -- the binary --------------------------------------------------------------
def binary() -> str | None:
    found = shutil.which(config.WACLI_BIN)
    if found:
        return found
    # A desktop launcher's PATH often lacks the one directory it installs to.
    local = Path.home() / ".local" / "bin" / config.WACLI_BIN
    return str(local) if local.is_file() else None


def _run(command: tuple[str, ...], args: list[str], *, write: bool = False, timeout: float = 20.0) -> Any:
    """Run one allow-listed wacli command as argv. Returns `data`, or raises."""
    if not (command in _ALLOWED_COMMANDS or (write and command == _SEND_COMMAND)):
        raise WacliError(f"{' '.join(command)} is not allowed")
    exe = binary()
    if exe is None:
        raise WacliError("wacli is not installed")
    argv = [exe, "--json"] + ([] if write else ["--read-only"]) + [*command, *args]
    env = {**os.environ, **({} if write else {"WACLI_READONLY": "1"})}
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WacliError("wacli timed out", uncertain=write) from exc
    except OSError as exc:
        raise WacliError(f"wacli would not run: {exc}") from exc
    try:
        loaded = json.loads(done.stdout or "{}")
    except ValueError:
        loaded = {}
    envelope = loaded if isinstance(loaded, dict) else {}
    failed = done.returncode != 0 or envelope.get("success") is False
    if failed:
        message = str(envelope.get("error") or done.stderr or f"exit {done.returncode}").strip()[:400]
        raise WacliError(message, latch=bool(_LATCH_WORDS.search(message)), uncertain=write)
    return envelope.get("data", envelope)


def _field(row: Any, *names: str) -> Any:
    """First present of several spellings. Docs show snake_case; be forgiving."""
    if not isinstance(row, dict):
        return None
    for name in names:
        if row.get(name) not in (None, ""):
            return row[name]
    return None


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    rows = data.get(key) if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


# -- state: the ledger and the latch -----------------------------------------
def _state_path() -> Path:
    return config.STATE_DIR / "whatsapp.json"


def _state() -> dict[str, Any]:
    raw = read_json(_state_path(), {})
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, Any]) -> bool:
    return write_json(_state_path(), state)


def latched() -> str:
    with _lock:
        return str(_state().get("latched") or "")


def _latch(reason: str) -> None:
    log.warning("WhatsApp sending latched off: %s", reason)
    with _lock:
        state = _state()
        state["latched"] = reason[:200]
        _save_state(state)


def reset_latch() -> str:
    """Only ever from `--whatsapp-reset`: no tool can let itself back out."""
    with _lock:
        state = _state()
        held = str(state.get("latched") or "")
        state["latched"] = ""
        state["errors"] = 0
        _save_state(state)
    return held


def _fingerprint(jid: str, text: str) -> str:
    # A hash, because a ledger of who got sent what is not worth keeping.
    flat = " ".join(text.lower().split())
    return hashlib.sha256(f"{jid}\0{flat}".encode("utf-8")).hexdigest()[:24]


def _limit_reason(jid: str, text: str, now: float) -> str:
    """Why this send may not go now, or ""."""
    held = _state()
    if held.get("latched"):
        return f"sending is latched off ({held['latched']})"
    rows = [row for row in held.get("sends", []) if isinstance(row, dict)]
    stamps = [float(row.get("at", 0)) for row in rows]
    if stamps and now - max(stamps) < config.WHATSAPP_MIN_GAP_S:
        return f"the last send was under {int(config.WHATSAPP_MIN_GAP_S)} seconds ago"
    if sum(1 for at in stamps if now - at < 3600) >= config.WHATSAPP_MAX_PER_HOUR:
        return f"{config.WHATSAPP_MAX_PER_HOUR} messages have gone in the last hour"
    if sum(1 for at in stamps if now - at < 86400) >= config.WHATSAPP_MAX_PER_DAY:
        return f"{config.WHATSAPP_MAX_PER_DAY} messages have gone today"
    print_ = _fingerprint(jid, text)
    for row in rows:
        if row.get("hash") == print_ and now - float(row.get("at", 0)) < config.WHATSAPP_REPEAT_WINDOW_S:
            return "that exact message already went to them"
    return ""


def _record_attempt(jid: str, text: str, now: float) -> bool:
    with _lock:
        state = _state()
        rows = [row for row in state.get("sends", []) if isinstance(row, dict)]
        rows = [row for row in rows if now - float(row.get("at", 0)) < 86400 * 2]
        rows.append({"at": now, "hash": _fingerprint(jid, text)})
        state["sends"] = rows
        return _save_state(state)


def _note_outcome(ok: bool) -> None:
    with _lock:
        state = _state()
        state["errors"] = 0 if ok else int(state.get("errors") or 0) + 1
        over = int(state["errors"]) >= config.WHATSAPP_MAX_ERRORS
        _save_state(state)
    if over:
        _latch(f"{config.WHATSAPP_MAX_ERRORS} send errors in a row")


def _first_use_note() -> str:
    """Said once: this is the user's call to know about, and third parties' text."""
    with _lock:
        state = _state()
        if state.get("disclosed"):
            return ""
        state["disclosed"] = True
        _save_state(state)
    return " Heads up: message text goes to the cloud model when I read or draft."


# -- sync ----------------------------------------------------------------
def ensure_sync() -> None:
    """One long-lived `sync --follow`, restarted at most once per backoff.

    Sends delegate to it, so there is one WhatsApp session rather than a new
    connection per message, and connection churn is the last thing to show.
    """
    if not config.WHATSAPP_SYNC_ENABLED:
        return
    from tools.guard import is_locked_down

    # Through importlib: `tools` exports a function of this name, which shadows
    # the module on the package.
    system_control = importlib.import_module("tools.system_control")

    # A read still works in lockdown; starting a process must not.
    if is_locked_down():
        return
    exe = binary()
    if exe is None:
        return
    with _lock:
        known = system_control.entries().get("whatsapp-sync")
        if known and system_control.is_alive(known):
            return
        state = _state()
        now = time.time()
        if now - float(state.get("sync_tried_at") or 0) < config.WHATSAPP_SYNC_BACKOFF_S:
            return
        state["sync_tried_at"] = now
        _save_state(state)
    logs = config.STATE_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / "whatsapp-sync.log"
    try:
        with log_path.open("ab") as sink:
            process = subprocess.Popen(
                [exe, "sync", "--follow"], stdin=subprocess.DEVNULL, stdout=sink,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
    except OSError as exc:
        log.warning("wacli sync would not start: %s", exc)
        return
    system_control.register("whatsapp-sync", process.pid, "wacli sync --follow", "", str(log_path), kind="whatsapp")


# -- contacts ----------------------------------------------------------------
def _contact_label(row: dict[str, Any]) -> str:
    name = _field(row, "alias", "full_name", "name", "push_name", "first_name", "business_name", "FullName", "Name")
    return str(name or _field(row, "phone", "jid", "JID") or "them")


def _resolve(contact: str) -> tuple[str, str, str]:
    """(jid, label, "") for one known person, or ("", "", why not)."""
    wanted = (contact or "").strip()
    if not wanted:
        return "", "", "no contact named"
    rows = _rows(_run(("contacts", "search"), [wanted, "--limit", "6"]), "contacts")
    people = [
        row for row in rows
        if str(_field(row, "jid", "JID") or "") and not str(_field(row, "jid", "JID")).endswith(_NOT_A_PERSON)
    ]
    exact = [row for row in people if _contact_label(row).lower() == wanted.lower()]
    picked = exact if len(exact) == 1 else people
    if not picked:
        return "", "", f"no contact matches {wanted!r}"
    if len(picked) > 1:
        names = ", ".join(_contact_label(row) for row in picked[:5])
        return "", "", f"AMBIGUOUS:{names}"
    return str(_field(picked[0], "jid", "JID")), _contact_label(picked[0]), ""


def _has_history(jid: str) -> bool:
    return bool(_rows(_run(("messages", "list"), ["--chat", jid, "--limit", "1"]), "messages"))


# -- the style of the user's own messages ------------------------------------
def _own_messages(jid: str = "") -> list[str]:
    args = ["--from-me", "--limit", str(config.WHATSAPP_STYLE_SAMPLES)]
    rows = _rows(_run(("messages", "list"), ["--chat", jid, *args] if jid else args), "messages")
    texts = [str(_field(row, "text", "display_text", "Text", "body") or "").strip() for row in rows]
    return [text for text in texts if text and not _URL.search(text)]


def style_notes(samples: list[str]) -> str:
    """What a person would notice about how these were written. Local."""
    if not samples:
        return ""
    count = len(samples)
    words = sum(len(text.split()) for text in samples) / count
    lower = sum(1 for text in samples if text == text.lower()) / count
    stops = sum(1 for text in samples if text.rstrip()[-1:] in ".!?") / count
    emoji = sum(1 for text in samples if _EMOJI.search(text)) / count
    examples = sorted(samples, key=len)[count // 4 :][: config.WHATSAPP_STYLE_EXAMPLES]
    listed = "\n".join(f"- {text[:160]}" for text in examples)
    return (
        f"Typically {words:.0f} words. All lowercase {lower:.0%} of the time, ends with "
        f"punctuation {stops:.0%}, uses emoji {emoji:.0%}.\nExamples:\n{listed}"
    )


def _draft(jid: str, intent: str) -> str:
    """The message in the user's voice. Falls back to their words as given."""
    from tools.web_agent import ask_planner

    try:
        samples = _own_messages(jid)
        if len(samples) < config.WHATSAPP_STYLE_MIN:
            samples = _own_messages()
        notes = style_notes(samples)
        if not notes:
            return intent
        reply = ask_planner(f"Style notes:\n{notes}\n\nThe user wants to say:\n{intent}", _DRAFT_SYSTEM)
        text = str(json.loads(reply).get("text") or "").strip()
        return text or intent
    except Exception as exc:
        log.debug("Drafting fell back to the user's own words: %s", exc)
        return intent


# -- actions -----------------------------------------------------------------
def _format_messages(rows: list[dict[str, Any]]) -> str:
    lines = []
    for row in rows:
        who = "me" if _field(row, "from_me", "FromMe") else str(
            _field(row, "sender_name", "sender", "chat_name", "SenderName") or "them"
        )
        when = _field(row, "timestamp", "ts", "Timestamp") or ""
        text = str(_field(row, "text", "display_text", "Text", "body") or "").strip()[:500]
        ident = _field(row, "msg_id", "id", "MsgID") or ""
        media = _field(row, "media_type", "type", "MediaType")
        chat = _field(row, "chat_jid", "chat", "ChatJID") or ""
        extra = f" [{media} chat={chat} id={ident}]" if media and media != "text" else ""
        lines.append(f"{when} {who}: {text}{extra}")
    return "\n".join(lines)


def _reading_failure(exc: WacliError) -> ToolResult:
    if binary() is None:
        return ToolResult.failure(
            "WhatsApp isn't set up yet.",
            "wacli is not installed. Tell the user; do not try to install it yourself.",
        )
    if exc.latch:
        return ToolResult.failure(
            "WhatsApp says this device isn't signed in.",
            f"{exc} The user must pair it themselves: run `wacli auth` and scan the code.",
        )
    return ToolResult.failure("WhatsApp wouldn't answer.", f"wacli: {exc}")


def _search(query: str, contact: str) -> ToolResult:
    text = query.strip()
    if not text:
        return ToolResult.failure("Search for what?", "search needs query.")
    args = [text, "--limit", str(config.WHATSAPP_READ_LIMIT)]
    if contact.strip():
        args += ["--chat", contact.strip()]
    rows = _rows(_run(("messages", "search"), args), "messages")
    if not rows:
        return ToolResult.success("Nothing in your chats on that.", f"No messages match {text!r}.")
    return ToolResult.success(
        f"Found {len(rows)}." + _first_use_note(), f"Messages matching {text!r}:\n{_format_messages(rows)}"
    )


def _recent(contact: str) -> ToolResult:
    args = ["--limit", str(config.WHATSAPP_READ_LIMIT)]
    if contact.strip():
        args += ["--chat", contact.strip()]
    rows = _rows(_run(("messages", "list"), args), "messages")
    if not rows:
        return ToolResult.success("No messages there.", "No stored messages matched.")
    return ToolResult.success(
        f"Got the last {len(rows)}." + _first_use_note(), f"Recent messages:\n{_format_messages(rows)}"
    )


def _downloads_dir() -> Path:
    from tools.file_manager import resolve_user_path

    return resolve_user_path(config.WHATSAPP_DOWNLOAD_DIR)


def _fetch(contact: str, message_id: str) -> ToolResult:
    from tools.file_manager import PathRefused, _check

    if not contact.strip() or not message_id.strip():
        return ToolResult.failure("Which file?", "fetch needs contact (the chat) and id, from a search result.")
    try:
        folder = _downloads_dir()
        folder.mkdir(parents=True, exist_ok=True)
    except (PathRefused, OSError) as exc:
        return ToolResult.failure("I've nowhere allowed to save it.", str(exc))
    data = _run(
        ("media", "download"),
        ["--chat", contact.strip(), "--id", message_id.strip(), "--output", str(folder)],
        timeout=120.0,
    )
    saved = _field(data, "path", "output", "file", "Path") if isinstance(data, dict) else None
    if not saved:
        return ToolResult.failure("That file didn't come through.", f"wacli reported no path: {data!r}"[:300])
    # The name came from whoever sent it, so it is not trusted until checked.
    try:
        path = _check(Path(str(saved)))
    except PathRefused as exc:
        return ToolResult.failure("That file landed somewhere it shouldn't.", str(exc))
    return ToolResult.success("Got the file.", f"Saved to {path}")


def _send_to_phone(raw_path: str, device: str, confirmed: bool) -> ToolResult:
    from tools.file_manager import PathRefused, friendly, resolve_user_path

    tool = shutil.which("kdeconnect-cli")
    if tool is None:
        return ToolResult.failure("KDE Connect isn't installed.", "kdeconnect-cli is not on PATH.")
    try:
        path = resolve_user_path(raw_path)
    except PathRefused as exc:
        return ToolResult.failure("I can't send that one.", str(exc))
    if not path.is_file():
        return ToolResult.failure("I can't find that file.", f"{path} is not a file.")
    if not confirmed:
        return ToolResult.confirm(
            f"Send {friendly(path)} to your phone? Confirm?",
            f"Waiting on a yes before sharing {path} over KDE Connect.",
            action="send_to_phone", path=str(path), device=device,
            reason="sends a file to your phone",
        )
    if not device:
        listed = subprocess.run(
            [tool, "--id-only", "-a"], capture_output=True, text=True, timeout=10.0, check=False
        )
        devices = listed.stdout.split()
        if not devices:
            return ToolResult.failure(
                "Your phone isn't connected.", "KDE Connect reports no reachable, paired device."
            )
        device = devices[0]
    done = subprocess.run(
        [tool, "--device", device, "--share", str(path)], capture_output=True, text=True, timeout=60.0, check=False
    )
    if done.returncode != 0:
        return ToolResult.failure("That didn't reach your phone.", done.stderr.strip()[:300])
    return ToolResult.success("Sent to your phone.", f"Shared {path} with device {device}.")


def _prepare_send(contact: str, message: str, exact: bool) -> ToolResult:
    intent = message.strip()
    if not intent:
        return ToolResult.failure("Say what?", "send needs message.")
    if latched():
        return ToolResult.failure(
            "WhatsApp sending is switched off.",
            f"Latched: {latched()}. Only the user can clear it, with --whatsapp-reset.",
        )
    jid, label, why = _resolve(contact)
    if not jid:
        if why.startswith("AMBIGUOUS:"):
            return ToolResult.failure(
                "More than one person matches. Which one?", f"Matches: {why[10:]}. Ask the user which."
            )
        return ToolResult.failure("I can't find them in your contacts.", why)
    if not _has_history(jid):
        return ToolResult.failure(
            "You've never chatted with them, so I won't start it.",
            "No message history with this contact. Cold messages are the likeliest way to be "
            "reported. Do not retry; the user can send this first one from their phone.",
        )
    text = intent if exact else _draft(jid, intent)
    if len(text) > config.WHATSAPP_MAX_CHARS:
        return ToolResult.failure(
            "That's too long to read back to you.",
            f"{len(text)} characters; the limit is {config.WHATSAPP_MAX_CHARS}, because every "
            "send is read aloud before it goes.",
        )
    blocked = _limit_reason(jid, text, time.time())
    if blocked:
        return ToolResult.failure(f"Not sending: {blocked}.", f"Send refused: {blocked}. Do not retry.")
    link = " It has a link in it." if _URL.search(text) else ""
    return ToolResult.confirm(
        f"To {label}: {text}.{link} Confirm?",
        f"Waiting on a spoken yes before sending to {label}. Text: {text}",
        action="send", jid=jid, label=label, draft=text,
        reason="sends something other people will see",
    )


def _do_send(jid: str, label: str, text: str) -> ToolResult:
    if jid.endswith(_NOT_A_PERSON):
        return ToolResult.failure("I only send to one person at a time.", "Groups are not supported.")
    now = time.time()
    # Everything is checked again: minutes can pass between question and yes.
    blocked = _limit_reason(jid, text, now)
    if blocked:
        return ToolResult.failure(f"Not sending: {blocked}.", f"Send refused: {blocked}. Do not retry.")
    if not _record_attempt(jid, text, now):
        return ToolResult.failure(
            "I couldn't log it, so I didn't send it.", "The send ledger would not write. Nothing was sent."
        )
    ensure_sync()
    try:
        _run(_SEND_COMMAND, ["--to", jid, "--message", text, "--no-preview"], write=True, timeout=45.0)
    except WacliError as exc:
        _note_outcome(False)
        if exc.latch:
            _latch(str(exc))
        return ToolResult.failure(
            f"That may not have reached {label}. I won't try again, so check your phone.",
            f"wacli: {exc}. The outcome is uncertain: it may have been delivered. Do not retry.",
        )
    _note_outcome(True)
    return ToolResult.success(f"Sent to {label}.", f"Sent to {label}: {text}")


def whatsapp(
    action: str = "",
    query: str = "",
    contact: str = "",
    message: str = "",
    exact: bool = False,
    id: str = "",  # noqa: A002 - wacli's own word for it, and the schema's
    path: str = "",
    device: str = "",
    jid: str = "",
    label: str = "",
    draft: str = "",
    confirmed: bool = False,
    **_: object,
) -> ToolResult:
    verb = (action or "recent").strip().lower()
    try:
        if verb == "send_to_phone":
            return _send_to_phone(path, device, confirmed)
        if verb == "send" and confirmed:
            if not jid or not draft:
                return ToolResult.failure("I've lost track of that message.", "A send replay had no target.")
            return _do_send(jid, label or "them", draft)
        if binary() is None:
            return _reading_failure(WacliError("not installed"))
        ensure_sync()
        if verb == "search":
            return _search(query, contact)
        if verb == "recent":
            return _recent(contact)
        if verb == "fetch":
            return _fetch(contact, id)
        if verb == "send":
            return _prepare_send(contact, message, bool(exact))
    except WacliError as exc:
        return _reading_failure(exc)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult.failure("WhatsApp wouldn't answer.", f"{verb}: {exc}")
    return ToolResult.failure("I can't do that on WhatsApp.", f"Unknown action {verb!r}.")


def check_lines() -> list[str]:
    """What `--check` prints. Reads only, and never starts a sync."""
    if not config.WHATSAPP_ENABLED:
        return []
    exe = binary()
    if exe is None:
        return ["WARN whatsapp: wacli not installed, so no chats"]
    lines: list[str] = []
    try:
        doctor = _run(("doctor",), [])
        auth = _field(doctor, "authenticated") if isinstance(doctor, dict) else None
        if auth is False:
            lines.append("WARN whatsapp: not paired. Run: wacli auth, then scan the code on your phone")
        else:
            lines.append("OK   whatsapp: wacli paired" if auth else "OK   whatsapp: wacli answered")
    except WacliError as exc:
        lines.append(f"WARN whatsapp: wacli did not answer ({exc})")
    held = latched()
    if held:
        lines.append(f"WARN whatsapp sending: latched off ({held}). Clear with --whatsapp-reset")
    else:
        lines.append(
            f"OK   whatsapp sending: every send asks; at most {config.WHATSAPP_MAX_PER_HOUR}/hour, "
            f"{config.WHATSAPP_MAX_PER_DAY}/day, known contacts only"
        )
    return lines
