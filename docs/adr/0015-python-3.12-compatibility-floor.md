# ADR-0015: Support Python 3.12 as a floor, alongside 3.13

## Status

Accepted

## Context

`requires-python` was `>=3.13,<3.14` — an exact-3.13 pin, not a floor —
since the project's start, matched by `uv.lock`/`pylock.toml`'s own
`requires-python = "==3.13.*"`. That was never load-bearing on any 3.13
language feature; nothing in this codebase used one (checked: no PEP 695
generic syntax, no `@override`, no 3.13-only stdlib/typing addition). It
was simply "use the current release," the same default this project
applies everywhere else absent a reason to pin lower.

An operator's air-gapped environment has Python 3.12 available and not
3.13 — a real, current constraint, not a hypothetical one. Building
`tools/verify_ucs_central.py`'s (and any future collector's) whole
dependency graph from `requirements.txt` fails outright under 3.12 while
`requires-python` reads `>=3.13,<3.14`: `pip` refuses to install a
package whose own declared floor the running interpreter doesn't meet.

## Decision

Widen `requires-python` to `>=3.12,<3.14`. `[tool.mypy]`'s
`python_version` moves from `3.13` to `3.12` — the floor, not the
CI/production interpreter — so type-checking actually catches a
3.13-only construct before it silently breaks the 3.12 path. `uv lock`,
then `uv export` for both `requirements.txt` and `pylock.toml`,
regenerate all three lock artifacts against the widened range.

**Verified, not assumed** — the standing bar this project holds every
choice to: a real Python 3.12.3 interpreter, `pip install -r
requirements.txt` (clean), `pip install -e . --no-deps` (clean), the
full 416-test unit suite, `ruff`, and `mypy` — all run against that 3.12
venv, all clean. The 3.13 path was re-run afterward (`uv sync`, same
suite/lint/typecheck) to confirm the lock regeneration didn't shift any
resolved version in a way that broke it either.

**The production container image is deliberately untouched.**
`Containerfile`/CI still build and test on 3.13 via
`python-build-standalone` (see `docs/air-gap.md`) — UBI9's own RPM
Python 3.12 package was already available as the "no extra interpreter
artifact" option when 3.13 was first chosen and wasn't taken, and
nothing about this ADR revisits that tradeoff. This is narrowly about
the *floor* `pyproject.toml` declares, so a pip-based install (this
platform's air-gapped `requirements.txt`/`pylock.toml` path, per
`docs/air-gap.md`) works on either interpreter a real deployment might
actually have. CI continues to test only 3.13; the 3.12 path has no
standing automated coverage beyond this ADR's one-time verification —
worth adding a CI job for if 3.12 support needs to stay proven over
time, not just true today.

## Consequences

- An air-gapped environment with only Python 3.12 can now build this
  project's full dependency graph from the mirrored `requirements.txt`/
  `pylock.toml` and run `tools/verify_ucs_central.py`,
  `tools/run_collector.py`, or the test/lint/typecheck suite locally.
- `requirements.txt`/`pylock.toml` grew (~110 lines each): resolving for
  two minor versions means both `cp312` and `cp313` wheel rows for every
  package with compiled extensions (`pydantic-core`, `regex`, etc.),
  where resolving for one interpreter needed only one.
- No CI job runs on 3.12. If that matters going forward — the same
  question ADR-0013's "Keeping CI current" pass should periodically
  ask — add a matrix entry rather than assuming this ADR's one-time
  local verification stays true indefinitely.
