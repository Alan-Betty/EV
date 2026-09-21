"""Terminal presentation for E.V.

A sci-fi console built on `rich`, with a hard rule that makes the whole module
safe: **nothing here ever returns a string that could reach the speaker.**

Every function is a `-> None` side effect on the terminal. Labels, panels,
prefixes, colours and spinners live here and only here; `Speaker.say` is fed
the raw reply text from a completely separate path in `ev_core`. That
separation is what guarantees E.V. never reads "E.V. >" or "Executing..."
aloud, and `tests/test_ui.py` asserts it rather than trusting it.

Degrades cleanly: if `rich` is not installed, or the output is redirected to a
file, or `EV_UI_PLAIN=true`, every call falls back to a plain `print` with the
same information and no escape codes.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Iterator

import config

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:  # rich is a nicety, never a requirement
    _RICH_AVAILABLE = False


def _can_encode(text: str) -> bool:
    """Can the current stdout actually represent these characters?

    A Windows console on the cp1252 code page cannot encode block-drawing
    glyphs, and `print` raises `UnicodeEncodeError` rather than degrading. An
    ASCII banner is a small price next to crashing on the first line of output.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# Block-capital "E.V." Drawn once at startup and never reprinted, so the cost
# is a single write.
HEADER_UNICODE = r"""
 ███████╗    ██╗   ██╗
 ██╔════╝    ██║   ██║
 █████╗      ██║   ██║
 ██╔══╝      ╚██╗ ██╔╝
 ███████╗██╗  ╚████╔╝ ██╗
 ╚══════╝╚═╝   ╚═══╝  ╚═╝
"""

# Same shape, drawn with characters every code page has.
HEADER_ASCII = r"""
  ______  __     __
 |  ____| \ \   / /
 | |__     \ \ / /
 |  __|     \ V /
 | |____ _   \_/   _
 |______(_)       (_)
"""

GLYPHS_UNICODE = {"tool": "\u2699", "ok": "\u2713", "bad": "\u2717", "dot": "\u00b7"}
GLYPHS_ASCII = {"tool": ">>", "ok": "+", "bad": "x", "dot": "-"}


# High-contrast, colour-blind-safe pairs that survive both light and dark
# terminals. Cyan is E.V., white is the user, amber is machine action.
STYLE_EV = "bold cyan"
STYLE_USER = "bold white"
STYLE_ACTION = "bold yellow"
STYLE_DIM = "dim"
STYLE_WARN = "bold yellow"
STYLE_ERROR = "bold red"
STYLE_OK = "bold green"


class UI:
    """The console. One instance per process, created by `ev_core`."""

    def __init__(self, plain: bool | None = None) -> None:
        forced_plain = config.UI_PLAIN if plain is None else plain
        self.rich = _RICH_AVAILABLE and not forced_plain and sys.stdout.isatty()
        self._console = Console(highlight=False, soft_wrap=False) if self.rich else None
        # Spinners are pointless when stdout is a pipe, and actively harmful
        # when something else is mid-prompt on the same line.
        self._spinners = self.rich and config.UI_SPINNERS
        # The one spinner that outlives a single call. `rich` allows exactly
        # one live display at a time, so it is held here and torn down by
        # anything that wants its own.
        self._live = None
        self._live_label = ""
        # Decided once: a console that cannot encode block glyphs would
        # otherwise raise on the banner before anything else happens.
        unicode_ok = _can_encode(HEADER_UNICODE + "".join(GLYPHS_UNICODE.values()))
        self.header_art = HEADER_UNICODE if unicode_ok else HEADER_ASCII
        self.glyphs = GLYPHS_UNICODE if unicode_ok else GLYPHS_ASCII

    # -- startup ----------------------------------------------------------
    def header(self, provider: str, voice: str, mode: str) -> None:
        """Draw the banner. Called once, before the loop starts."""
        if not self.rich:
            print(self.header_art)
            print(f"  E.V.  -  Everyday Virtual Assistant")
            print(f"  cloud brain, local hands")
            print(f"  brain: {provider}")
            print(f"  voice: {voice}")
            print(f"  mode:  {mode}\n")
            return

        banner = Text(self.header_art, style=STYLE_EV)
        banner.append("  EVERYDAY VIRTUAL ASSISTANT\n", style="bold cyan")
        banner.append("  cloud brain, local hands\n\n", style="dim cyan")
        banner.append("  brain  ", style="dim")
        banner.append(f"{provider}\n", style="white")
        banner.append("  voice  ", style="dim")
        banner.append(f"{voice}\n", style="white")
        banner.append("  mode   ", style="dim")
        banner.append(f"{mode}", style="white")
        self._console.print(
            Panel(banner, border_style="cyan", padding=(0, 2), title="[bold cyan]E.V.[/]")
        )

    def hint(self, text: str) -> None:
        """A one-off usage note under the banner."""
        if not self.rich:
            print(text)
            return
        self._console.print(f"[dim]{text}[/]")

    # -- conversation -----------------------------------------------------
    def user(self, text: str, engaged: bool = False) -> None:
        """Echo what the user said. The dot marks an open conversation."""
        marker = "." if engaged else " "
        if not self.rich:
            print(f"you {marker}> {text}")
            return
        line = Text()
        line.append(f"you {marker}> ", style=STYLE_DIM)
        line.append(text, style=STYLE_USER)
        self._console.print(line)

    def speech(self, text: str) -> None:
        """Show what E.V. is saying.

        The styling lives entirely in this function. `text` is displayed but
        never modified, and the same unmodified string is what the speaker
        receives on its own path - the terminal decoration cannot leak into it.
        """
        if not self.rich:
            print(f"E.V. > {text}")
            return
        self._console.print(
            Panel(
                Text(text, style=STYLE_EV),
                border_style="cyan",
                padding=(0, 1),
                title="[bold cyan]E.V.[/]",
                title_align="left",
            )
        )

    # -- machine activity -------------------------------------------------
    def action(self, tool: str, summary: str = "") -> None:
        """Announce a tool invocation, in the machine-action colour."""
        if not self.rich:
            print(f"  [{tool}] {summary}".rstrip())
            return
        line = Text()
        line.append(f"  {self.glyphs['tool']} ", style=STYLE_ACTION)
        line.append(tool, style=STYLE_ACTION)
        if summary:
            line.append(f"  {summary}", style=STYLE_DIM)
        self._console.print(line)

    def note(self, text: str) -> None:
        if not self.rich:
            print(f"[E.V.] {text}")
            return
        self._console.print(f"[dim]  {self.glyphs['dot']} {text}[/]")

    def ok(self, text: str) -> None:
        if not self.rich:
            print(f"[E.V.] {text}")
            return
        self._console.print(f"[{STYLE_OK}]  {self.glyphs['ok']}[/] [dim]{text}[/]")

    def warn(self, text: str) -> None:
        if not self.rich:
            print(f"[E.V.] {text}")
            return
        self._console.print(f"[{STYLE_WARN}]  ![/] [yellow]{text}[/]")

    def error(self, text: str) -> None:
        if not self.rich:
            print(f"[E.V.] {text}", file=sys.stderr)
            return
        self._console.print(f"[{STYLE_ERROR}]  {self.glyphs['bad']} {text}[/]")

    def rule(self, label: str = "") -> None:
        if not self.rich:
            print("-" * 60)
            return
        self._console.rule(f"[dim]{label}[/]" if label else "", style="dim cyan")

    # -- status spinner ---------------------------------------------------
    @contextlib.contextmanager
    def status(self, label: str) -> Iterator[None]:
        """Animated `[Thinking...]` / `[Transcribing...]` / `[Executing...]`.

        A context manager so the spinner is always torn down, including on an
        exception, leaving the cursor where the next line expects it.
        """
        if not self._spinners:
            yield
            return
        # `rich` permits one live display at a time, so a spinner that is
        # being held open elsewhere is retired rather than fought with.
        self.end_status()
        with self._console.status(
            f"[cyan]{label}[/]", spinner="dots", spinner_style="cyan"
        ):
            yield

    def begin_status(self, label: str) -> None:
        """Start a spinner that outlives the call that started it.

        The listening spinner spans a whole conversation window, which is
        several trips round the loop: E.V. polls the microphone on a short
        timeout so the window can expire, and a spinner torn down and rebuilt
        every couple of seconds is a flicker rather than an animation. Calling
        this again with the same label is deliberately a no-op.
        """
        if not self._spinners:
            return
        if self._live is not None:
            if self._live_label == label:
                return
            self.end_status()
        try:
            live = self._console.status(
                f"[cyan]{label}[/]", spinner="dots", spinner_style="cyan"
            )
            live.start()
        except Exception:  # pragma: no cover - a spinner is never load bearing
            return
        self._live, self._live_label = live, label

    def end_status(self) -> None:
        """Take down the long-running spinner, if there is one. Never raises."""
        live, self._live = self._live, None
        self._live_label = ""
        if live is None:
            return
        try:
            live.stop()
        except Exception:  # pragma: no cover - teardown races
            pass

    def prompt(self, engaged: bool, standby: bool) -> str:
        """The text-mode input prompt. Returned, not printed, for `input()`."""
        if standby:
            return "standby > "
        return "you . > " if engaged else "you   > "


_ui: UI | None = None


def get_ui() -> UI:
    """The process-wide console."""
    global _ui
    if _ui is None:
        _ui = UI()
    return _ui
