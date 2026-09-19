"""Command risk classification and confirmation gating.

The LLM is not trusted to decide what is safe to run. Every shell command
passes through `classify` first, and anything that looks destructive is
blocked outright or held for an explicit human confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Risk(str, Enum):
    SAFE = "safe"
    REVIEW = "review"  # plausible but needs a yes/no from the user
    BLOCKED = "blocked"  # never run, regardless of confirmation


@dataclass(frozen=True)
class Verdict:
    risk: Risk
    reason: str

    @property
    def needs_confirmation(self) -> bool:
        return self.risk is Risk.REVIEW

    @property
    def is_blocked(self) -> bool:
        return self.risk is Risk.BLOCKED


# Patterns that are never worth the risk of a misheard voice command.
_BLOCKED: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+/\s*$", "recursive delete of root"),
    (r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+[a-z]:[\\/]?\s*$", "recursive delete of a drive root"),
    (r"\bformat\s+[a-z]:", "disk format"),
    (r"\bdiskpart\b", "raw disk partitioning"),
    (r"\bmkfs(\.\w+)?\b", "filesystem creation"),
    (r"\bdd\b[^|]*\bof=/dev/", "raw device write"),
    (r"\bdel\s+/[sq][^|]*\\\*", "recursive wildcard delete"),
    (r"remove-item[^|]*-recurse[^|]*\b[a-z]:\\?\s*$", "recursive delete of a drive root"),
    (r"\bcipher\s+/w", "free-space wipe"),
    (r"\bvssadmin\s+delete\s+shadows", "shadow copy deletion"),
    (r"\bbcdedit\b", "boot configuration change"),
    (r":\(\)\s*\{.*\}\s*;?\s*:", "fork bomb"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "permission wipe on root"),
    (r"(curl|wget|iwr|invoke-webrequest)[^|]*\|\s*(sudo\s+)?(ba)?sh\b", "pipe-to-shell from the internet"),
    (r"(curl|wget|iwr|invoke-webrequest)[^|]*\|\s*(iex|invoke-expression)", "pipe-to-shell from the internet"),
    (r"\b(iex|invoke-expression)\s*\(\s*(new-object\s+net\.webclient|iwr|invoke-webrequest)", "remote code execution"),
)

# Patterns that are legitimate but destructive enough to need a yes.
_REVIEW: tuple[tuple[str, str], ...] = (
    (r"\brm\b", "deletes files"),
    (r"\bdel\b", "deletes files"),
    (r"\berase\b", "deletes files"),
    (r"\bremove-item\b", "deletes files"),
    (r"\brmdir\b|\brd\b", "removes a directory"),
    (r"\bmv\b|\bmove\b|\bmove-item\b", "moves or overwrites files"),
    (r"\bshutdown\b|\brestart-computer\b|\bstop-computer\b", "shuts down or reboots the machine"),
    (r"\breg\s+(delete|add)\b|\bset-itemproperty\s+-path\s+hk", "edits the registry"),
    (r"\btaskkill\b|\bstop-process\b|\bkill\b|\bpkill\b", "terminates processes"),
    (r"\bnet\s+(user|localgroup)\b", "changes user accounts"),
    (r"\bicacls\b|\btakeown\b", "changes file permissions"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--)\b", "discards or publishes git work"),
    (r"\bnetsh\b|\bfirewall\b", "changes network or firewall settings"),
    (r"\bsc\s+(delete|stop|config)\b|\bset-service\b|\bstop-service\b", "changes Windows services"),
    (r"\b(pip|npm|yarn|pnpm|choco|winget|apt|apt-get)\s+(install|uninstall|remove|add)\b", "installs or removes software"),
    (r"\bsudo\b|\bstart-process[^|]*-verb\s+runas", "runs with elevated privileges"),
    (r"\bset-executionpolicy\b", "changes PowerShell execution policy"),
    (r"\bschtasks\b|\bregister-scheduledtask\b", "creates a scheduled task"),
    (r">\s*[^>|\s]+", "redirects output over a file"),
)


# ---------------------------------------------------------------------------
# GUI actions
# ---------------------------------------------------------------------------
# Driving the mouse and keyboard is a second risk surface, and the shell
# patterns above say nothing useful about it: no regex over a command line
# will ever notice that the button under the pointer says "Place order".
#
# What we do have is the description of what is about to happen - the label
# on the button, the text being typed, the goal of the run. That is the
# string these patterns read. It is not a sandbox and it proves nothing; it
# is the difference between a misread screen costing a click and a misread
# screen costing a purchase.
#
# Nothing here is BLOCKED. A GUI action has no equivalent of "format C:" that
# is never legitimate - a user may genuinely want the thing bought, sent or
# deleted. What they must not get is it happening without being asked.
_GUI_REVIEW: tuple[tuple[str, str], ...] = (
    (
        r"\b(buy|purchase|checkout|check\s+out|pay|payment|place\s+(the\s+)?order|"
        r"complete\s+(the\s+)?(order|purchase)|proceed\s+to\s+(checkout|payment)|"
        r"confirm\s+(and\s+)?(pay|purchase|order)|subscribe|renew\b|upgrade\s+plan)\b",
        "spends money",
    ),
    (
        r"\b(send|sending|post|publish|tweet|submit|reply\s+all|forward)\b",
        "sends something other people will see",
    ),
    (
        r"\b(delete|deleting|remove|discard|erase|wipe|uninstall|"
        r"empty\s+(the\s+)?(bin|trash|recycle\s+bin)|move\s+to\s+trash|"
        r"factory\s+reset|format\s+(the\s+)?(disk|drive))\b",
        "destroys something",
    ),
    (
        r"\b(password|passphrase|credit\s*card|card\s+number|cvv|cvc|"
        r"security\s+code|social\s+security|two[\s-]factor|one[\s-]time\s+code|"
        r"seed\s+phrase|private\s+key)\b",
        "handles a credential",
    ),
    (
        r"\b(sign\s+out|log\s+out|shut\s*down|reboot|power\s+off|restart\s+the\s+"
        r"(pc|computer|machine))\b",
        "ends the session or the machine",
    ),
    (
        r"\b(grant\s+access|allow\s+access|authorise|authorize|"
        r"accept\s+(the\s+)?(terms|invite|request)|agree\s+to)\b",
        "agrees to something on the user's behalf",
    ),
    (
        r"\balt\s*\+\s*f4\b|\bctrl\s*\+\s*alt\s*\+\s*del(ete)?\b",
        "closes a window or interrupts the system",
    ),
)


def classify_gui(description: str) -> Verdict:
    """Classify a GUI action by how it was described.

    `description` is whatever names the intent: the label on the button, the
    text about to be typed, the hotkey, the goal of an autonomous run.
    Callers pass everything they have, joined, because the risk can live in
    any one of them - "click Confirm" is harmless right up until the page
    underneath it is a checkout.

    Typed text is *also* put through `classify`, separately, so a shell
    command typed into a terminal window meets the same blocked patterns it
    would have met had it been run directly. This function deliberately does
    not do that itself: the shell REVIEW list flags every ordinary sentence
    containing the word "move", which is fine for a command line and useless
    for a description of a click.
    """
    text = " ".join((description or "").lower().split())
    if not text:
        return Verdict(Risk.SAFE, "nothing to classify")

    for pattern, reason in _GUI_REVIEW:
        if re.search(pattern, text):
            return Verdict(Risk.REVIEW, reason)

    return Verdict(Risk.SAFE, "no risky intent matched")


def classify(command: str) -> Verdict:
    """Classify a shell command by how much damage it could do."""
    text = " ".join(command.lower().split())
    if not text:
        return Verdict(Risk.BLOCKED, "empty command")

    for pattern, reason in _BLOCKED:
        if re.search(pattern, text):
            return Verdict(Risk.BLOCKED, reason)

    for pattern, reason in _REVIEW:
        if re.search(pattern, text):
            return Verdict(Risk.REVIEW, reason)

    return Verdict(Risk.SAFE, "no destructive pattern matched")


_AFFIRMATIVE = {
    "yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "do it",
    "go ahead", "go", "affirmative", "ok", "okay", "please do", "run it",
    "y", "proceed",
}

_NEGATIVE = {
    "no", "nope", "nah", "cancel", "stop", "abort", "don't", "dont", "negative",
    "never mind", "nevermind", "n", "forget it",
}


def is_negative(text: str) -> bool:
    """True for a clear no.

    Distinct from `not is_affirmative(...)`: anything that is neither a yes
    nor a no is the user moving on to something else, and the caller needs to
    tell those apart so an unrelated request is not swallowed as a refusal.
    """
    cleaned = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if not cleaned:
        return False
    if cleaned in _NEGATIVE:
        return True
    words = cleaned.split()
    return words[0] in _NEGATIVE or " ".join(words[:2]) in _NEGATIVE


def is_affirmative(text: str) -> bool:
    """True only for a clear yes. Anything ambiguous counts as a no."""
    cleaned = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if not cleaned:
        return False
    if cleaned in _AFFIRMATIVE:
        return True
    # Allow a leading yes with trailing words ("yes, do it"), but never if a
    # negation appears anywhere in the utterance.
    if any(word in cleaned.split() for word in _NEGATIVE):
        return False
    first_two = " ".join(cleaned.split()[:2])
    return cleaned.split()[0] in _AFFIRMATIVE or first_two in _AFFIRMATIVE
