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

## Update (2026-08-16): pinned versions lowered to what the mirror actually carries

The operator's air-gapped PyPI mirror doesn't carry the exact versions
this project had pinned: `redis` 8.1.0, `ucsmsdk` 0.9.27, `ucscsdk`
0.9.0.10, `testcontainers` 4.15.0. All four moved down to what the
mirror has — `redis==8.0.1`, `ucsmsdk==0.9.18`, `ucscsdk==0.9.0.8`,
`testcontainers==4.14.2` — and `[tool.ruff]`'s `target-version` moved
from `py313` to `py312` alongside `[tool.mypy]`'s `python_version`, for
the same reason: targeting the CI interpreter rather than the floor
would let `ruff --select UP` suggest a 3.13-only construct.

**The `ucsmsdk`/`ucscsdk` downgrade is not a routine bump for this
project** — ADR-0009 and ADR-0014's entire methodology is "confirmed
against the *installed* package source," at exact pinned versions, down
to `mo_meta.parents` (DN depth) and `presence` enum values. Lowering the
pin without re-checking would quietly invalidate that work. So it was
checked, not assumed: every class and attribute `ucs_manager`/
`ucs_central`/`ucs_common` reads — `ComputeBlade`/`ComputeRackUnit`/
`ComputeBoard`/`LsServer`/`MgmtIf`/`AdaptorHostEthIf`/`AdaptorExtEthIf`/
`ProcessorUnit`/`StorageController`/`StorageLocalDisk`/`ComputeSystem`/
`LsSPMeta`/`InventoryDomainEp` — was diffed programmatically between
0.9.27/0.9.0.10 and 0.9.18/0.9.0.8: same `mo_meta.parents` for every
class, same `presence` enum values (including the `equipped-slave`/
`equipped-not-primary` non-primary set), same `device_type`/`disk_state`
constants, no missing field anywhere this codebase reads one. Byte-
identical on the entire surface this project touches.

(One asymmetry, present in *both* old and new `ucsmsdk` — not a
version regression: `ucsmsdk`'s generated `StorageLocalDiskConsts` has
never had a `DEVICE_TYPE_NVME` constant, unlike `ucscsdk`'s. Harmless
here either way — the mapping compares the lowercase wire string, never
the SDK's own constant.)

`uv.lock`/`requirements.txt`/`pylock.toml` regenerated again for the
lower pins, and the full verification from the update above repeated
against them exactly — a fresh Python 3.12.3 venv, `pip install -r
requirements.txt`, editable install, all 416 unit tests, `ruff`, `mypy`:
clean. 3.13 re-verified again afterward too. The only new artifact is a
`SyntaxWarning: invalid escape sequence '\h'` from inside `ucsmsdk`
0.9.18's own `ucssession.py` (an unescaped backslash in a string
literal, in the *vendor's* source, not ours) — cosmetic, harmless, does
not fail anything, and not something a pin change in this repo can fix.

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
