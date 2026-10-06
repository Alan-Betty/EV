"""What every desktop backend hands back: windows, and the elements inside them.

One shape for every OS, so the tools above never branch on where a window
came from. A backend that cannot know something says so in the data -
`geometry=False` for a GNOME Wayland window known only through
accessibility, `bounds=None` for an element whose position is
window-relative - rather than inventing a rectangle the model would then
click on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WindowInfo:
    """One top-level window, as the window manager sees it.

    The rectangle is in real screen pixels. `fractions` converts it to the
    0-1 coordinate space the vision tools speak, because a model that is
    told "Notepad is at 0.12,0.08 to 0.72,0.83" can click inside Notepad
    without having to estimate anything from a downscaled JPEG.

    `hwnd` is the backend's own handle - an HWND on Windows, an X window
    id, a Mutter window id, an AT-SPI object path - and is opaque to
    everything except the backend named in `source`.
    """

    hwnd: Any
    title: str
    class_name: str
    left: int
    top: int
    right: int
    bottom: int
    focused: bool = False
    minimized: bool = False
    pid: int = 0
    app: str = ""
    source: str = ""
    geometry: bool = True

    @property
    def id(self) -> Any:
        return self.hwnd

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def fractions(self, screen_width: int, screen_height: int) -> tuple[float, float, float, float]:
        if screen_width <= 0 or screen_height <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (
            round(self.left / screen_width, 3),
            round(self.top / screen_height, 3),
            round(self.right / screen_width, 3),
            round(self.bottom / screen_height, 3),
        )

    def matches(self, needle: str) -> bool:
        """Case-insensitive match on the title, the app name or the class."""
        wanted = (needle or "").strip().lower()
        if not wanted:
            return False
        return any(wanted in (text or "").lower() for text in (self.title, self.app, self.class_name))


# ---------------------------------------------------------------------------
# Accessibility
# ---------------------------------------------------------------------------
@dataclass
class Element:
    """One thing inside a window that a person could read or operate.

    `ref` is the number the planner uses ("press 12"). It is assigned when
    the tree is read and is only good until the next read, exactly like the
    `data-ev` numbers the browser route stamps on a page - which is why the
    history records the label next to it.
    """

    ref: int
    role: str
    name: str = ""
    value: str = ""
    description: str = ""
    states: frozenset[str] = frozenset()
    actions: tuple[str, ...] = ()
    bounds: tuple[int, int, int, int] | None = None
    depth: int = 0
    # The backend's own handle on the element - an AT-SPI (bus name, path)
    # pair, a UIA element - so an action can find it again.
    handle: Any = field(default=None, repr=False, compare=False)

    @property
    def label(self) -> str:
        return self.name or self.description or self.value

    @property
    def enabled(self) -> bool:
        return "disabled" not in self.states

    def describe(self) -> str:
        """`12 button "Save"`, `7 text "Search" = "foo"`, `3 menu "File" [collapsed]`."""
        text = f"{self.ref} {self.role}"
        if self.name:
            text += f' "{_clip(self.name, 60)}"'
        elif self.description:
            text += f' "{_clip(self.description, 60)}"'
        if self.value and self.value != self.name:
            text += f' = "{_clip(self.value, 50)}"'
        marks = sorted(self.states & _SHOWN_STATES)
        if marks:
            text += " [" + ", ".join(marks) + "]"
        return text


_SHOWN_STATES = frozenset(
    {"checked", "unchecked", "selected", "expanded", "collapsed", "focused", "disabled", "pressed", "modal"}
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Tree:
    """The readable, operable part of one window."""

    window: str
    app: str = ""
    elements: list[Element] = field(default_factory=list)
    truncated: bool = False
    sandboxed: bool = False
    source: str = ""
    error: str = ""

    def by_ref(self, ref: int) -> Element | None:
        for element in self.elements:
            if element.ref == ref:
                return element
        return None

    @property
    def actionable(self) -> list[Element]:
        return [element for element in self.elements if element.actions or element.role in EDITABLE_ROLES]


EDITABLE_ROLES = frozenset(
    {"text", "entry", "edit", "password text", "document", "combo box", "spin button", "slider"}
)
