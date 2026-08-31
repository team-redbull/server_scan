# ADR-0019: ty replaces mypy as this repository's type checker

**Status:** Accepted 2026-08-31, fully implemented 2026-09-01. mypy is
gone; ty is the gate. The exit criterion for the flip was shortened
from three green runs to one by the repository owner — see "Staged
rollout" for what that traded away.

**Relates to** `docs/adr/0013-supply-chain-pinning-without-dependabot.md`
(nothing here updates itself, so everything here is pinned and enters the
quarterly manual pass) and
`docs/adr/0015-python-3.12-compatibility-floor.md` (the floor both
checkers target).

## Context

This repository has been type-checked by mypy 1.14.1 under `strict = true`
since it began, with the pydantic plugin loaded, over `backend/app tools`
— 130 files, ~19,300 lines. It is clean and has been kept clean.

Astral's ty is a type checker written in Rust from the same project as
ruff, which this repository already depends on. The question is whether
swapping one for the other is worth doing on current merit, which
Convention 1 requires be answered by measurement rather than by
enthusiasm for the ecosystem.

The decision was taken in two steps: a measurement pass with a stop point,
then the migration. The numbers below are from that pass, run against
ty 0.0.76 (2026-08-26) and mypy 1.14.1 on this codebase, not from
benchmarks published elsewhere.

## What was measured

### Diagnostics

mypy reports **0 errors**. ty reports:

| Configuration | Diagnostics |
|---|---|
| ty, default rules | 8 |
| ty, the mypy-strict-equivalent rule subset | 21 |
| ty, `--error all` (191 rules) | 23 |

All 23 fall in twelve code sites, classified individually:

| Class | Count | Share |
|---|---|---|
| Genuine — ty is right, mypy misses it | 7 | 30% |
| Pre-existing, already suppressed under mypy | 1 | 4% |
| False positives from ty's incomplete coverage | 5 | 22% |
| `Any`-flow noise from opt-in rules with no mypy analogue | 8 | 35% |
| A style convention this repo has not adopted | 2 | 9% |

**At the default rule severities the split is 7 genuine, 1 suppression
mismatch, and zero false positives.** That is the number the decision
rests on. The configured rule set in `[tool.ty.rules]` reproduces it.

The 7 genuine findings are one site: `IngestService._build_server` built
four fields into a `dict[str, object]` and `**`-unpacked it into
`Server(...)`, so every keyword it landed on arrived as `object`. mypy
does not check `**`-unpack value types against parameter types. Fixing it
also revealed that the splat had been suppressing mypy's own
required-argument checking for the whole call.

The brief for the measurement pass predicted a flood of findings from ty
checking unannotated function bodies that mypy skips. **There were none**
— this codebase is fully annotated under `disallow_untyped_defs`, so mypy
was skipping nothing for ty to newly discover. That prediction is correct
in general and simply does not apply here; a codebase with untyped
regions should expect a very different first run.

### Speed

| | Cold | Warm |
|---|---|---|
| mypy 1.14.1 | 15.12 s | 0.31 s |
| ty 0.0.76 | 0.17 s | 0.16 s |

ty 0.0.76 keeps no persistent on-disk cache, so cold and warm are the same
number. CI runs cold every time, which is where the ~89× matters.

### Third-party typing

**Pydantic.** ty models the synthesized `__init__` natively, including
pydantic's lax coercion types, with no plugin. It catches a wrong-typed
field at construction; **mypy with `plugins = ["pydantic.mypy"]` does
not**, because `init_typed` was never enabled — the plugin as configured
here bought this repository nothing that ty does not do without it.
ty's one pydantic gap is real: `model_dump()` resolves to `Unknown` where
mypy has `dict[str, Any]`. It is the most-called pydantic method in this
codebase and the direct cause of 2 of the 5 false positives. It costs
nothing at the configured severities.

**Async PyMongo** (this repo uses async PyMongo, not Motor — ADR-0003).
Parity. Identical inference for `find`, `find_one`, and
`AsyncCollection.__getattr__`; both catch a wrong-typed awaited result;
both miss a bogus keyword argument to `find_one`.

**The Cisco SDKs.** `ucsmsdk`/`ucscsdk` ship no stubs and are not
`py.typed`, so mypy has them under `ignore_missing_imports` — i.e. `Any`,
i.e. unchecked. ty resolves them from installed source and catches a call
to a method that does not exist. This is new coverage over the two
collectors that matter most, and it is the reason the CI exit criterion
below is what it is.

### Async correctness

The gate's protection against a blocking call inside `async def` was never
mypy's, which was worth establishing before assuming a regression:

| | mypy | ty |
|---|---|---|
| `time.sleep()` in `async def` | misses | misses |
| Missing `await` | error | `unused-awaitable`, pinned to `error` here |
| `await` on a non-awaitable | error | error |
| Sync `for` over an `AsyncCursor` | **false positive** | correct |

`time.sleep()` in async code is caught by ruff `ASYNC251`, already
selected. Nothing moves.

### Suppression comments

**ty honours a bare `# type: ignore` but not a coded one.** It does not
know mypy's rule codes, so `# type: ignore[return-value]` suppresses
nothing. Both can share a line provided mypy's comes first; ty reads
either position, mypy only the leading one.

This repository had two suppressions inside the checked paths, so the
conversion was two lines. **That is the main reason this migration was
cheap, and it will not generalise.** A codebase with hundreds of coded
ignores would find this single incompatibility dominating the entire
migration, and should budget for it before starting.

### What mypy catches that ty cannot

One thing, and it is not small: **`disallow_untyped_defs`.** None of ty's
191 rules require a function to be annotated, and ty cannot grow one
without giving up the gradual guarantee that makes it predictable — it
infers unannotated bodies rather than rejecting them.

Everything else has an equivalent: `mypy_path`/`explicit_package_bases` →
`environment.root`; `python_version` → `environment.python-version`
(ty also infers it correctly from `requires-python`); `warn_return_any` →
`unsound-return-statement`; `disallow_any_generics` →
`missing-type-argument`; `[[tool.mypy.overrides]]` →
`[[tool.ty.overrides]]`. The `ignore_missing_imports` overrides for
`testcontainers`, `ucsmsdk` and `ucscsdk` turn out to be unnecessary —
ty resolved all three from source with no `unresolved-import`. Stub
packages (`types-regex`) are consumed identically.

### Three mypy false positives

Worth recording, because "we are leaving the stricter tool" is the wrong
summary of this change:

1. Sync iteration over an `AsyncCursor` is reported as an error.
   pymongo's `AsyncCursor.__getitem__(int)` exists, so legacy-protocol
   iteration is legal Python. ty is right.
2. `Server(_id=...)` is reported as missing the argument `id`.
   `Server.model_config` sets `populate_by_name`, so the alias is valid;
   the pydantic plugin only reads `model_config` when it is a
   `ConfigDict(...)` call, not the plain dict literal this model uses.
   Was suppressed narrowly in `ingest.py`; deleted with mypy.
3. The pydantic plugin's silence on wrong-typed field construction —
   not an error it emits, but coverage it is widely assumed to provide
   and does not, here.

## Decision

**Adopt ty, and remove mypy — conditional on the annotation ratchet
moving to ruff first, which it has.**

### 1. Why ty over mypy

Not speed alone; that is a convenience. The load-bearing reasons are that
ty finds a real class of error mypy structurally cannot (calls into
untyped vendor SDKs, which is where two of this platform's three
collectors live), it understands pydantic better than the plugin this
repository was actually running, and it retires two standing mypy false
positives. The 30%-genuine / 0%-false-positive split at default severities
is the evidence; a different codebase could easily measure differently.

### 2. Why not Pyrefly or Pyright

Convention 1 requires the alternatives be considered rather than assumed
away.

**Pyright** is the most complete of the three and would have been the
conservative choice. It is rejected on operational fit, not capability:
it is a Node application, and this platform's build and air-gapped mirror
are Python-and-uv. Adding a Node toolchain to the backend lint job to
replace a `uv sync` dependency is a real cost paid every CI run and every
air-gap mirror refresh, for a checker whose findings on this codebase
would substantially overlap ty's. Its strict mode also has no
`disallow_untyped_defs` equivalent that is meaningfully different from
what ruff `ANN` now provides.

**Pyrefly** (Meta, also Rust, also fast) is a genuine peer and was the
closest call. It is rejected for ecosystem coherence: this repository
already depends on ruff and uv, both Astral, and a single vendor for
lint + format + type + package management means one pin story, one
config file, one upgrade pass, and one project's release cadence to track
on the quarterly chore — rather than two independent 0.x tools each able
to break the build on their own schedule. Pyrefly is also pre-1.0, so
choosing it would not have avoided the beta risk, only doubled the number
of vendors carrying it.

Both remain viable if the rollback trigger below fires.

### 3. The exact pin, and why exact

`ty==0.0.76`, with `==`, never `>=` and never a floating install.

ty is beta on 0.0.x versioning and Astral state plainly that it has no
stable API and that diagnostics may change between any two releases. A
range would let an unrelated `uv sync` fail the build with no change on
our side, which is precisely the failure mode ADR-0013's SHA pins exist
to prevent. Pinning makes a bump a deliberate act, and makes the rule
unambiguous: **a new diagnostic after a version bump is ty changing, not
this codebase regressing.**

The pin is not a workaround for the beta risk; it is the containment for
it. It converts an unpredictable failure into a scheduled one.

### 4. Rules switched off, and why

`[tool.ty.rules]` writes down only the rules this project has reasoned
about. The rest ride on ty's defaults — a deliberate line, since the exact
pin already prevents defaults from moving underneath us, and enumerating
191 rules would be documentation theatre.

**On:**

- `unused-awaitable = "error"` — mypy reported a missing `await` as an
  error and this keeps that. (ty exits 1 on warnings anyway;
  `terminal.error-on-warning` defaults to true. Making it explicit is
  cheaper than depending on that.)
- `blanket-ignore-comment = "error"` — an uncoded `# ty: ignore` hides
  more than it should. Currently clean.

**Off:**

- `unsound-return-statement`, `unsound-assignment`,
  `missing-type-argument` — these fire wherever `Any` reaches a declared
  type. That is 15 sites here: 13 are `Any` arriving from an untyped
  vendor SDK via `getattr`, or from Starlette's `request.state` /
  `app.state`, all deliberate and documented; 2 are ty's `model_dump()`
  gap. mypy's `strict` (which includes `warn_return_any` and
  `disallow_any_generics`) caught none of them either. Enabling them
  would cost 15 suppressions and buy no coverage the gate previously had.
- `missing-override-decorator` — PEP 698 `@override` is a convention this
  repository has not adopted. Adopting it is its own change, on its own
  merits, not a side effect of changing type checker.

Four of these six are written down **specifically because ty 0.0.76's
published rules reference disagrees with the shipped binary about their
default severity** — the docs call them errors, the binary ignores them.
Trusting the documentation would have put 15 findings straight into the
gate. This is the 0.0.x risk showing up on day one, in the mildest
possible form, and it is the concrete argument for pinning severities
rather than inheriting them.

`ANN401` is likewise not selected in ruff: `Any` at an untyped-vendor-SDK
boundary is this codebase's documented design, and selecting it would
flag 37 such sites.

### 5. The precondition: ruff `ANN`

**Removing mypy removes `disallow_untyped_defs`, and ty has no
replacement at any setting.** Without something in its place, the next
collector lands with unannotated helpers, ty infers them happily, and both
checkers stay green — a permanent, silent reduction in what the gate
protects, discovered by nobody.

ruff `ANN001`, `ANN002`, `ANN003`, `ANN201`, `ANN202`, `ANN204`, `ANN205`
and `ANN206` are that replacement, and ruff is already in the gate,
already pinned, already the right shape. `ANN002`/`ANN003` are included
deliberately: `disallow_untyped_defs` requires `*args`/`**kwargs` to be
annotated too, and given that the one genuine bug this migration found was
a `**`-unpack with an under-specified type, leaving that half of the
ratchet off would be a strange place to relax.

Enabling it cost seven annotations — one in `backend/app`, six in
`tests/`, which mypy never checked because it runs on `backend/app tools`.
Four of the seven carried a `# type: ignore[no-untyped-def]` that a real
annotation retires.

The codes are listed individually rather than selecting the `ANN` prefix,
and `[tool.ruff.lint.flake8-annotations]` is written out at its current
defaults, for the same reason the ty severities are: an upstream default
must not be able to move this gate silently.

**This is a hard precondition on Phase 4, not a nice-to-have. Do not
remove mypy while it is the only thing enforcing annotations.**

### 6. Staged rollout

1. **Ruff `ANN` ratchet** — landed. The precondition, first.
2. **ty added alongside mypy**, pinned, configured; mypy still the gate.
   The two genuine findings fixed. Both checkers pass. — landed.
3. **ty in CI, non-blocking** (`continue-on-error: true`), as a fourth
   step in the `lint` job. mypy still the gate. — landed, v8.2.0.
4. **Flip the gate**: remove `continue-on-error`, remove the mypy step,
   the `mypy==1.14.1` dev dependency, `[tool.mypy]`, its two overrides,
   and the two mypy-only suppressions this ADR names. Regenerate the
   air-gap exports. Carries a `BREAKING CHANGE:` footer — CI reads
   Conventional Commits to version the published images, and changing
   the gate changes the contributor contract. — landed.
5. **Convention** — CLAUDE.md, README and docs updated to the ty command.
   — landed with 4.

**The exit criterion for step 3 → 4 was three consecutive green `lint`
runs on `main`. It was shortened to one, deliberately, by the repository
owner.** That is recorded rather than glossed because the criterion is
still the right default for anyone repeating this.

The reasoning for three was never "wait out ty releases" — with an exact
`==` pin ty cannot change underneath us, so that would insure against a
risk the pin already eliminated. It was the one thing the pin does not
cover: ty resolves types from *installed source* rather than from stubs
alone, which is how it found the `ucsmsdk` call error, and which makes its
diagnostics a function of what is actually in the environment. CI's venv
is built fresh from `uv.lock`; a developer's is not.

The first green run answered exactly that question — CI's `ty` step
reported `All checks passed!` in **0.3 s** against a freshly resolved
environment, matching local byte for byte. Runs 2 and 3 would have been
confirming stability rather than testing a hypothesis, which is the
weaker half of the evidence. **What was given up is the chance to catch a
non-deterministic difference** — a resolver picking a different transitive
version on a later run, say. If one appears, it will now show up as a red
build rather than as a warning annotation, which is a louder failure but
a later one. §8's rollback triggers are unchanged and still apply.

### 6b. Stub packages are a checker dependency too

Removing mypy raised the question of whether `types-regex` was a mypy
artifact. It is not: **ty consumes typeshed stub packages exactly as mypy
did**, and without it the `Pattern` subscript in
`app.domain.services.regex_engine` is unresolvable. It stays.

It was also the one *unpinned* dev dependency, and an unrelated `uv sync`
during this work bumped it from `2026.7.19.20260720` to
`2026.8.31.20260831` on its own. That is the same failure mode the `ty`
pin exists to prevent — a stub package can move a type checker's
diagnostics precisely the way a checker version can, and this one
publishes a fresh datestamped release most weeks. It is now pinned
exactly, and joins ty on the quarterly pass.

### 7. Air-gap

ty is a dev dependency, so it never reaches the runtime image:
`requirements.txt` and `pylock.toml` are both exported `--no-dev` and are
byte-unchanged by this work, exactly as they have always been for mypy.

For a mirror that does carry dev dependencies, ty ships as platform
wheels rather than an sdist, so the right one has to be present. Confirmed
for this deployment's target:
`ty-0.0.76-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`
resolves for `x86_64-manylinux2014` — a glibc 2.17 floor against UBI9's
2.34 — and the `py3-none` tag means there is no interpreter ABI to match.
`musllinux` and `aarch64` wheels also exist if either becomes relevant.

ty is installed as that dev dependency and **not** as a GitHub Action:
nothing new to SHA-pin under ADR-0013, and the version CI runs is the
version `uv.lock` names, which is the version a developer runs locally.

### 8. Rollback

Revert to mypy as the gate if any of these hold:

- ty's diagnostics differ between CI and local for the same commit — the
  environment-sensitivity risk the exit criterion is testing for.
- A ty version bump on the quarterly pass produces findings that cannot be
  resolved as either genuine or narrowly suppressible within one sitting.
- ty's `model_dump()` → `Unknown` gap widens into something that fires at
  the configured severities.
- A real defect ships that mypy's `strict` would have caught.

Rollback is cheap and stays cheap: it is restoring a dev dependency and a
CI step, and `[tool.mypy]` should be recovered from this commit range
rather than rewritten. The ruff `ANN` ratchet stays regardless — it is
correct independently of which type checker runs.

## Consequences

- The local gate keeps its three-step shape; step three becomes
  `uv run ty check backend/app tools`.
- CI's `lint` job runs four steps until the flip, three after it.
- Cold type-checking in CI drops from ~15 s to well under a second.
- Contributors write `# type: ignore[x]  # ty: ignore[y]` (mypy's first)
  until the flip, and `# ty: ignore[y]` alone after it.
- ty is 0.0.x and pinned, so it joins the "Keeping CI current" quarterly
  pass. **Expect diagnostics to change between versions; a new error after
  a bump is ty changing, not a regression in this codebase.** That
  sentence is the whole containment strategy and belongs in the chore
  itself, not only here.
- `tests/` remains outside the type checker's scope, as it has always
  been, but is now inside the annotation ratchet's scope, which it was
  not.
