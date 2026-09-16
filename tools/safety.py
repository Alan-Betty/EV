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
