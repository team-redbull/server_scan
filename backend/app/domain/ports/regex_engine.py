"""The `RegexEngine` port.

Declared here so `domain.services.classification` depends only on this
Protocol, never on a specific regex library — swapping the implementation
(e.g. to Google's RE2 for a stricter linear-time guarantee, at the cost of
losing backreferences/lookarounds) is a one-line change at the call site,
not a rewrite of the classification engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RegexTimeout(Exception):
    """Raised when a pattern's match against a subject exceeds the
    configured time budget. Callers (the classification engine) catch
    this per-rule and quarantine the offending rule rather than letting
    one pathological pattern stall an entire classification run.
    """


class RegexUnsafeError(Exception):
    """Raised at compile time (not match time) for a pattern rejected
    before it's ever run — too long, or fails a canary timing probe
    against known-pathological inputs.
    """


@dataclass(frozen=True, slots=True)
class RegexMatch:
    start: int
    end: int


class RegexEngine(Protocol):
    def validate(self, pattern: str, *, ignore_case: bool, multiline: bool, dotall: bool) -> None:
        """Raise `RegexUnsafeError` if `pattern` should never be accepted
        (too long, fails a timeout canary). Called at rule write time.
        """
        ...

    def search(
        self,
        pattern: str,
        subject: str,
        *,
        ignore_case: bool,
        multiline: bool,
        dotall: bool,
    ) -> RegexMatch | None:
        """Raise `RegexTimeout` if matching exceeds the configured budget."""
        ...
