"""The desktop section of `python ev_core.py --check`.

Every tier here degrades quietly at run time - a window list with no
rectangles, an app that publishes nothing, input that only reaches XWayland
windows - which is right for a voice assistant and wrong for a readiness
report. This is where each of those is said out loud, with the fix.
"""

from __future__ import annotations

import importlib.util

import config
from tools.desktop import hands, system


def lines() -> list[tuple[str, str]]:
    """(status, text) pairs; status is OK, WARN or MISS."""
    out: list[tuple[str, str]] = []

    def add(status: str, text: str) -> None:
        out.append((status, text))

    try:
        info = hands.report()
    except Exception as exc:  # noqa: BLE001 - a report must not crash
        add("WARN", f"desktop: could not inspect the session ({exc})")
        return out
    add("OK", f"desktop: {info.get('session', system.os_name())}")
    hands_name = info.get("input", "?")
    if "warning" in info:
        add("WARN", f"desktop input: {hands_name} - {info['warning']}")
    else:
        add("OK", f"desktop input: {hands_name}, capture: {info.get('capture', '?')}")
    if "uinput" in info and hands_name == "uinput":
        add("OK" if info["uinput"] == "writable" else "MISS", f"uinput: {info['uinput']}")

    from tools import window

    try:
        listing = window.backend_names()
    except Exception:  # noqa: BLE001
        listing = []
    if not listing:
        add("WARN", "windows: no backend can list windows on this desktop")
    elif listing == ["x11", "atspi"] and system.desktop() == "gnome":
        add(
            "WARN",
            "windows: XWayland windows plus accessibility only - no positions, and focus "
            "goes through the overview. For exact windows: python ev_core.py "
            "--install-gnome-extension, then log out and back in",
        )
    else:
        add("OK", f"windows: {' + '.join(listing)}")

    if not getattr(config, "A11Y_ENABLED", True):
        add("WARN", "accessibility: off (EV_A11Y_ENABLED=false) - every in-app job uses vision")
        return out
    if system.IS_LINUX:
        if not importlib.util.find_spec("jeepney"):
            add("MISS", "accessibility: pip install jeepney - without it every in-app job uses vision")
            return out
        from tools.desktop import atspi

        if not atspi.available():
            add("WARN", "accessibility: the AT-SPI bus is not reachable")
        elif atspi.is_enabled():
            add("OK", "accessibility: AT-SPI on")
        else:
            add(
                "WARN",
                "accessibility: AT-SPI reachable, but toolkit accessibility is off - Chromium, "
                "Electron and Qt apps publish nothing until it is on. E.V. turns it on at first "
                "use (EV_A11Y_AUTO_ENABLE); apps already open then need a restart",
            )
        if system.snap_confined():
            add(
                "WARN",
                f"sandbox: E.V. runs confined ({system.confinement_label()}) - snapped apps may refuse "
                "to be read. Start E.V. from a normal terminal, not VS Code's",
            )
    elif system.IS_WINDOWS:
        if importlib.util.find_spec("comtypes"):
            add("OK", "accessibility: UI Automation (comtypes)")
        else:
            add("WARN", "accessibility: pip install comtypes - without it every in-app job uses vision")
    elif system.IS_MAC:
        from tools.desktop import macos

        if macos.available():
            add(
                "OK",
                "accessibility: System Events - the app running E.V. needs Accessibility "
                "permission (System Settings > Privacy & Security > Accessibility)",
            )
        else:
            add("WARN", "accessibility: osascript not found")
    return out
