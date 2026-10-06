"""The session bus, through `jeepney`, with a timeout on everything.

`media.py` and `ev/face/busy.py` shell out to `gdbus`, which is right for one
call every few seconds and wrong for this: a screenshot portal round trip is
a call plus a signal, and an accessibility tree is hundreds of calls. A
process per call would cost more than the work. `jeepney` is pure Python
(~200 KB, no compiled parts), imported lazily, and a missing install costs
the capability rather than the assistant - callers get `BusUnavailable`.

Every call carries a timeout because the other end is an arbitrary
application. A frozen app does not refuse an AT-SPI call; it simply never
answers, and without a deadline it takes E.V. down with it.
"""

from __future__ import annotations

import logging
import secrets
import threading
from typing import Any

log = logging.getLogger("ev.tools.desktop.bus")

DEFAULT_TIMEOUT_S = 2.0


class BusUnavailable(RuntimeError):
    """No session bus, or no jeepney to talk to it with."""


class BusCallError(RuntimeError):
    """The other end answered with a D-Bus error."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name
        self.message = message

    @property
    def access_denied(self) -> bool:
        """AppArmor (a snap on either end) refused the call outright."""
        text = f"{self.name} {self.message}".lower()
        return "accessdenied" in text or "apparmor" in text


_lock = threading.Lock()
_connection: Any = None

# One connection is shared by every tool thread, and a jeepney blocking
# connection is not safe to use from two threads at once: replies would be
# read by whichever caller happened to be waiting.
_call_lock = threading.RLock()


def connection() -> Any:
    """The shared blocking connection to the session bus, opened on first use."""
    global _connection
    with _lock:
        if _connection is not None:
            return _connection
        try:
            from jeepney.io.blocking import open_dbus_connection
        except ImportError as exc:
            raise BusUnavailable("jeepney is not installed (pip install jeepney)") from exc
        try:
            _connection = open_dbus_connection(bus="SESSION")
        except Exception as exc:
            raise BusUnavailable(f"no session bus: {exc}") from exc
        return _connection


def close() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:  # pragma: no cover - closing is best-effort
                pass
        _connection = None


def unique_name() -> str:
    return str(connection().unique_name or "")


def call(
    destination: str,
    path: str,
    interface: str,
    method: str,
    signature: str = "",
    body: tuple = (),
    timeout: float = DEFAULT_TIMEOUT_S,
    conn: Any = None,
) -> tuple:
    """One method call. Returns the reply body; raises `BusCallError`.

    `conn` lets the accessibility bus - a separate bus with its own
    address - reuse the same error and timeout handling.
    """
    from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call

    target = conn or connection()
    message = new_method_call(DBusAddress(path, destination, interface), method, signature, body)
    try:
        with _call_lock:
            reply = target.send_and_get_reply(message, timeout=timeout)
    except TimeoutError as exc:
        raise BusCallError("Timeout", f"{interface}.{method} did not answer in {timeout:g}s") from exc
    if reply.header.message_type == MessageType.error:
        name = str(reply.header.fields.get(HeaderFields.error_name, "error"))
        text = str(reply.body[0]) if reply.body else ""
        raise BusCallError(name, text)
    return tuple(reply.body)


def get_property(
    destination: str,
    path: str,
    interface: str,
    name: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    conn: Any = None,
) -> Any:
    """One property, with the variant unwrapped."""
    (value,) = call(
        destination, path, "org.freedesktop.DBus.Properties", "Get", "ss",
        (interface, name), timeout=timeout, conn=conn,
    )
    return unwrap(value)


def unwrap(value: Any) -> Any:
    """jeepney hands a variant back as (signature, value)."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


def portal_request(
    interface: str,
    method: str,
    signature: str,
    body_without_options: tuple,
    options: dict[str, tuple[str, Any]],
    timeout: float,
    path: str = "/org/freedesktop/portal/desktop",
) -> tuple[int, dict[str, Any]]:
    """Call a portal method and wait for its `Request.Response` signal.

    Portals answer asynchronously: the call returns a request handle, and
    the result arrives later as a signal on that handle. The handle is
    predictable from our unique name and a `handle_token` we choose, so the
    match rule goes on *before* the call - subscribing after it races the
    reply and loses on a fast desktop.

    Returns (response code, results). 0 is success, 1 the user cancelled,
    2 anything else.
    """
    from jeepney import MatchRule

    token = "ev" + secrets.token_hex(6)
    sender = unique_name().lstrip(":").replace(".", "_")
    handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    rule = MatchRule(
        type="signal",
        interface="org.freedesktop.portal.Request",
        member="Response",
        path=handle,
    )
    full_options = dict(options)
    full_options["handle_token"] = ("s", token)

    signal = call_and_wait_signal(
        rule,
        lambda: call(
            "org.freedesktop.portal.Desktop", path, interface, method,
            signature, (*body_without_options, full_options), timeout=timeout,
        ),
        timeout=timeout,
    )
    code, results = signal.body
    return int(code), {key: unwrap(value) for key, value in dict(results).items()}


def call_and_wait_signal(rule: Any, trigger: Any, timeout: float) -> Any:
    """Subscribe to `rule`, run `trigger()`, and return the first matching signal.

    The subscription goes on first because the signal can arrive before the
    call that caused it has even returned.
    """
    conn = connection()
    add = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
    with _call_lock:
        call(*add, "AddMatch", "s", (rule.serialise(),))
        try:
            with conn.filter(rule) as queue:
                trigger()
                try:
                    return conn.recv_until_filtered(queue, timeout=timeout)
                except TimeoutError as exc:
                    raise BusCallError("Timeout", "the expected signal never arrived") from exc
        finally:
            try:
                call(*add, "RemoveMatch", "s", (rule.serialise(),))
            except Exception:  # pragma: no cover - the rule dies with the connection anyway
                pass
