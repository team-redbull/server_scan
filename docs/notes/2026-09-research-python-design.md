# Production Python design, applied to server_scan

Research note for the production-hardening pass on branch `dev-refactor`.
No source code was changed by this note. Every claim below is either a
citation (PEP, CPython docs, CPython/installed-package source) or a probe
I ran against this repo's own toolchain and recorded verbatim.

**Toolchain the probes ran on** (measured 2026-09-06):
`ty 0.0.76` (pinned in `pyproject.toml:66`), project interpreter
CPython 3.13.12 (`~/.local/share/uv/python/cpython-3.13.12-linux-x86_64-gnu`),
`pydantic 2.13.4`, `requires-python = ">=3.12,<3.14"`.

**Repo shape this note is about**, read before writing anything:

| Symbol | File | Note |
|---|---|---|
| `ServerInventoryProvider` (3-member `Protocol`) | `backend/app/domain/ports/provider.py:173` | `provider_type: str`, `health_check`, `list_servers` |
| `ProviderServer`, `ProviderNic`, `ProviderAttachment` | same file, `:28`–`:170` | `@dataclass(frozen=True, slots=True)` |
| 7 implementations | `backend/app/infrastructure/providers/{fake,ucs_manager,ucs_central,intersight,openmanage,oneview,redfish}/provider.py` | **none imports the Protocol for inheritance** — every reference to it is in a docstring |
| `_NameFilteredProvider` (an 8th implementation, a decorator) | `tools/run_collector.py:457` | sets `self.provider_type` in `__init__` |
| `collection_errors_of` | `tools/run_collector.py:515` | `getattr(provider, "collection_errors", ())` — the optional capability, read reflectively |
| second reflective read of the same capability | `backend/app/infrastructure/providers/openmanage/provider.py:205` | `getattr(redfish, "collection_errors", ())` |
| ~10 inline stub providers | `tests/unit/tools/test_run_collector.py`, `tests/integration/test_ingest_partial_reads.py:34` | 3-line classes; **`tests/` is outside `ty check backend/app tools`** |
| sibling ports | `backend/app/domain/ports/repository.py:51`, `credentials.py:64`, `regex_engine.py:37`; `backend/app/application/services/ingest.py:89,99` | five more `Protocol`s, same house style |

---

## 1. ABC vs `Protocol` — the headline question

### 1.1 What each one is, per the specs

PEP 544 (*Protocols: Structural subtyping*) introduces `Protocol` and is
explicit that it does **not** replace nominal typing:

> "Both nominal and structural subtyping have their strengths and
> weaknesses. Therefore, in this PEP we *do not propose* to replace the
> nominal subtyping described by PEP 484 with structural subtyping
> completely." … "protocol classes as specified in this PEP complement
> normal classes, and users are free to choose where to apply a
> particular solution."

PEP 3119 (*Introducing Abstract Base Classes*) is equally explicit that
ABCs are for *signalling and enforcing intent*, not for replacing duck
typing:

> "ABCs are simply Python classes that are added into an object's
> inheritance tree to signal certain features of that object to an
> external inspector."
> "If `hasattr(x, "__len__")` works for you, great! ABCs are intended to
> solve problems that don't have a good solution at all in Python 2, such
> as distinguishing between mappings and sequences."

And the enforcement mechanism, PEP 3119 §"The abc module":

> "If the resulting `__abstractmethods__` set is non-empty, the class is
> considered abstract, and attempts to instantiate it will raise
> `TypeError`." … "abstract methods as defined here may have an
> implementation. This implementation can be called via the `super`
> mechanism."

So: `Protocol` = a *predicate over shapes*, checked at the point of use.
ABC = a *place in the MRO*, checked at the point of construction.

### 1.2 What ty 0.0.76 actually does — probed, not assumed

I ran five probes through `uv run ty check`. Verbatim results:

**(a) A structural implementer missing a member is caught at the call site,
with a good message:**

```
error[invalid-argument-type]: Argument to function `take` is incorrect
info: type `MissingAttr` is not assignable to protocol `P`
info: └── protocol member `provider_type` is not defined on type `MissingAttr`
```

**(b) …and so is a typo'd method name** (`health_chek` → `protocol member
`health_check` is not defined`) **and a wrong signature**:

```
info: type `WrongSig` is not assignable to protocol `P`
info: └── protocol member `health_check` is incompatible
info:     └── incompatible return types: `str` is not assignable to `CoroutineType[Any, Any, None]`
```

**(c) A class that sets `provider_type` only in `__init__` satisfies the
Protocol.** No diagnostic. This matters: `_NameFilteredProvider`
(`tools/run_collector.py:475`) does exactly that and is fine.

**(d) ty does *not* statically flag instantiating an abstract class.**
Probe: `class Forgot(Base): pass` where `Base` has one `@abstractmethod`,
then `_ = Forgot()`. ty 0.0.76 emitted **zero** diagnostics for that line
(it emitted 2 unrelated ones in the same file, so the file was checked).
At runtime the same line raises:

```
TypeError: Can't instantiate abstract class Forgot without an implementation for abstract method 'list_servers'
```

This is the single most important asymmetry for this decision, and it is a
*ty-specific* fact (mypy has `abstract` and flags it). **With ty, an ABC's
"did you implement it" enforcement is a runtime check, not a CI check.**
It fires at provider construction — i.e. inside `_build_provider`
(`tools/run_collector.py:541`), on the CronJob, in production — unless a
test instantiates every provider first. `tests/unit/tools/test_run_collector.py:101`
(`TestBuildProvider`) does construct each one, so in this repo it would in
fact fail in CI. That is a property of the test suite, not of the type
system, and it should be stated as such rather than assumed.

**(e) The async-generator override trap — a real, load-bearing detail.**
Every provider's `list_servers` is an *async generator function*
(`async def` + `yield`). The Protocol at `provider.py:178` declares it as

```python
def list_servers(self) -> AsyncIterator[ProviderServer]: ...
```

— **not** `async def`. That is correct and must be preserved. If a base
class declared it `async def`, every one of the 7 providers becomes a
Liskov violation. ty says so precisely:

```
error[invalid-method-override]: Invalid override of method `list_servers`
info: incompatible return types: `AsyncIterator[int]` is not assignable to `CoroutineType[Any, Any, AsyncIterator[int]]`
info: This violates the Liskov Substitution Principle
```

An `async def f() -> AsyncIterator[X]` *with* a `yield` has type
`() -> AsyncGenerator[X, None]`; the same signature *without* a yield (an
abstract stub, `...`) has type `() -> Coroutine[Any, Any, AsyncIterator[X]]`.
They are not the same type, and the abstract stub is the wrong one. This
trap costs nothing today because the Protocol got it right; it is exactly
the kind of thing an ABC conversion breaks silently-looking-loudly.

### 1.3 Runtime checking: `runtime_checkable` is weaker than it looks

CPython docs, `typing.runtime_checkable`:

> "`@runtime_checkable` will check only the presence of the required
> methods or attributes, **not their type signatures or types**."

The docstring in the installed `typing.py` is blunter:

> "Warning: this will check only the presence of the required methods,
> not their type signatures!"

Probed against a class with a *completely wrong* shape
(`health_check(self, extra: int) -> str`, `list_servers(self) -> int`):

```
runtime_checkable accepts a structurally wrong class: True
```

And PEP 544's `issubclass` restriction bites this exact Protocol, because
`provider_type: str` is a non-method member:

```
TypeError: Protocols with non-method members don't support issubclass(). Non-method members: 'provider_type'.
```

ty catches that statically, which is a nice touch:

```
error[isinstance-against-protocol]: Class `RC` cannot be used as the second argument to `issubclass`
info: A protocol class cannot be used in `issubclass` checks if it has non-method members
```

Two more current-docs facts worth having on file: since 3.12 protocol
`isinstance` uses `inspect.getattr_static` rather than `hasattr`, so
"some objects which used to be considered instances … may no longer be";
and the docs warn an `isinstance` against a runtime protocol "can be
surprisingly slow compared to an `isinstance()` check against a
non-protocol class."

**Conclusion:** `runtime_checkable` is not a substitute for the ABC's
runtime guarantee. It answers "are the names there", never "is the shape
right". An ABC answers "did you implement it" exactly, at construction.

### 1.4 `Protocol` with default method bodies does *not* give free behaviour

This is the option most people reach for to get "shared behaviour without
inheritance", and PEP 544 forecloses it:

> "The default implementations cannot be used if the subtype relationship
> is implicit and only via structural subtyping – the semantics of
> inheritance is not changed."

Worse — and I probed this, because it is the part people don't expect — a
protocol method *with* a default body is still a **required member** for
structural conformance. Adding `def describe(self) -> str: return ...` to
a Protocol immediately broke two previously-conforming classes:

```
info: type `WrongSig` is not assignable to protocol `P`
info: └── protocol member `describe` is not defined on type `WrongSig`
```

So a default body on a Protocol is the worst of both: structural
implementers must write the method themselves *anyway*, and only explicit
subclasses get the free one.

And explicit subclassing of a Protocol is *weak* enforcement. Probe:

```
Explicit() ok, list_servers() -> None
mro ['Explicit', 'P', 'Protocol', 'Generic', 'object']
```

`class Explicit(P): provider_type = "y"` — implementing nothing —
instantiates fine and `list_servers()` silently returns `None`, because
Protocol members declared with a `...` body are not `@abstractmethod`s.
A caller doing `async for x in provider.list_servers()` gets
`TypeError: 'async for' requires an object with __aiter__`, at collection
time, on the CronJob. That is a materially worse error than the ABC's
`TypeError: Can't instantiate abstract class …`.

### 1.5 The decision rule

**ABC (`abc.ABC` + `@abstractmethod`) wins when:**

- there is **shared behaviour** you actually want inherited (template
  method, `AbstractAsyncContextManager`'s `__aenter__` returning `self`);
- you want a **hard runtime failure at construction** when someone adds an
  implementation and forgets a member — "the constructor is the gate";
- implementations are **few, in-repo, and yours**, so the coupling of a
  shared base costs nothing;
- you want `isinstance` to mean something (`register()` gives you virtual
  subclasses, PEP 3119: "after the call `B.register(C)`, the call
  `issubclass(C, B)` will return True").

**Where an ABC is wrong:**

- when implementers are **outside your control** (third-party, or types
  you don't own) — you can't make `dict` inherit your base, only
  `register()` it, which re-opens every gap the ABC was for;
- when the base becomes a **place to put things** — an ABC with 3 abstract
  members and 9 concrete helpers is a god-class waiting to happen, and
  every provider now inherits the union of all vendors' conveniences;
- when it forces **multiple inheritance** to compose two orthogonal
  capabilities;
- when the class is a **test double**: an ABC makes every 3-line stub
  either inherit a base it doesn't need or lie.

**`Protocol` wins when:**

- implementers are **pre-existing or foreign** — this is the whole point
  of `ServerRepository` at `backend/app/domain/ports/repository.py:51`,
  whose docstring already says so;
- the **dependency direction** must not invert: `domain/` must not be
  imported *by* something it conceptually owns. A Protocol lets
  `infrastructure/` depend on nothing;
- **stubs and fakes** are cheap — the 10 inline test classes in
  `tests/unit/tools/test_run_collector.py` exist *because* the seam is
  structural;
- the contract is **narrow** (1–3 members) and has no behaviour to share.

**Where a `Protocol` is wrong:**

- when you need **runtime enforcement** of completeness (see 1.3 — you
  don't get it, `runtime_checkable` or not);
- when you have **real shared behaviour** (see 1.4 — you can't share it);
- when the contract is **wide**, because a structural check is
  all-or-nothing at the use site and the error names one member at a time;
- when *nothing in the codebase ever annotates a parameter with it*. A
  Protocol that is never used as an annotation is checked by nobody. (Not
  the case here — `ingest.py:294` and five sites in `run_collector.py`
  annotate it, which is why the current seam works at all.)

---

## 2. The principles, and where each one is wrong here

Each entry: the useful version, then the failure mode **in this codebase**.

### KISS
*Useful:* the cheapest structure that survives the next change.
*Wrong here:* "simple" is not "small". `ProviderServer`'s `None`-means-
unread convention (`provider.py:96`–`106`) is more complex than `int` and
is the correct simplicity — the simpler version wrote zeros over good data
and flipped a server CRITICAL→HEALTHY. Do not "simplify" a distinction
that a production incident bought.

### SOLID, letter by letter

**S — Single responsibility.** The real formulation is *one reason to
change*, i.e. one *audience*. `IngestService` has one audience (the
pipeline's owner). `redfish/mapping.py` has one audience (whoever reads
DMTF's schema).
*Wrong here:* applied per-function-length, it splits
`compute_unit_to_provider_server` (`ucs_manager/mapping.py:580`) — see §5.

**O — Open/closed.** Adding a vendor must not edit the pipeline. This is
already true and is the repo's best structural property:
`PROVIDER_FACTORIES` (`tools/run_collector.py:369`) is the single
extension point, and the guard in
`tests/unit/infrastructure/providers/test_generator.py` derives from it
rather than restating it.
*Wrong here:* `ManagerType` is a **closed enum on purpose**, and sites are
a closed runtime set (ADR-0018). Do not "open" configuration for
extensibility that the deployment model forbids.

**L — Liskov.** The one letter with teeth in this pass: §1.2(e) is a
Liskov violation ty will name out loud if the base is written carelessly.
Also: `_NameFilteredProvider` is a substitutability test — it must be
usable *everywhere* a provider is, which is why it forwards
`collection_errors` (`run_collector.py:481`) rather than swallowing it.
*Wrong here:* nowhere. Take this one literally.

**I — Interface segregation.** Directly relevant: `collection_errors` is a
capability only fan-out collectors have (UCS Central, Intersight,
OpenManage, Redfish — not OneView, not fake). §6 is the whole discussion.
*Wrong here:* segregating too eagerly gives you `HasHealthCheck`,
`ListsServers`, `ReportsErrors`, `HasProviderType` and a call site that
annotates the intersection. Four one-member protocols is worse than one
four-member one.

**D — Dependency inversion.** Already done: `domain/ports/*` declares,
`infrastructure/*` implements, and `domain/` imports nothing from
`infrastructure/`.
*Wrong here:* inverting *within* infrastructure. `openmanage` importing
`redfish` directly (`openmanage/provider.py`, the `redfish_for` factory at
`run_collector.py:122`) is correct — that is composition of two concrete
things, and a port between them would be ceremony.

### YAGNI / "simplest thing that could possibly work"
*Wrong here, specifically:* the provider seam **was** YAGNI when written —
one fake implementation, one Protocol. It is now load-bearing for 7. The
lesson is not "YAGNI was wrong"; it is that a seam is cheap when it is 3
members wide. Keep the width, not the abstraction count.

### Separation of concerns
*Useful:* `_NameFilteredProvider`'s docstring is the best statement of it
in the repo — filtering is a *collection* concern, not a *pipeline* one,
which is why it wraps rather than living in `IngestService`.
*Wrong here:* separating vendor facts from vendor mappings. Everything
UCSPE taught about `mgmtIf` belongs next to the code that reads `mgmtIf`
(or in `docs/cisco-collectors.md` with provenance — CLAUDE.md convention
8), never in a "shared field utilities" module.

### Code for the maintainer
*Useful:* this repo already over-indexes on it; the docstring convention
exists because inline prose became unreadable.
*Wrong here:* writing for a maintainer who does not exist. A generic
`ProviderBase` "so a future contributor has somewhere obvious to start" is
speculative maintenance.

### Avoid premature optimization
*Wrong here:* the two places where optimization is **not** premature and
is already load-bearing — Intersight's flat-in-fleet-size join strategy
with `$select` (ADR-0017), and OneView's `expand=all` (3 calls per
appliance instead of 3 per server). Both look like premature optimization
and are the difference between a 10k-server run finishing and not. Any
refactor that "simplifies" either is a regression; the note in
`oneview/provider.py:180` says so.

### Optimize for deletion
*Useful:* the best argument in the whole brief, and it favours structural
typing. A `Protocol` implementer is deletable by deleting its directory
and its `PROVIDER_FACTORIES` row. An ABC subclass is deletable the same
way *unless* the base grew a method for it.
*Wrong here:* deletability is not the only axis. `ProviderServer`'s field
set is deliberately additive and shared by 7 collectors; optimizing it for
deletion would mean per-vendor DTOs, which is the thing the port exists to
prevent.

### DRY, and wrong-abstraction coupling
*Useful:* `openmanage` reusing `redfish`'s mapping rather than writing a
second Dell mapping (ADR-0020) — one implementation, one provenance.
*Wrong here — the live example:* ADR-0022 records that HPE deliberately
**does not** copy Dell's OME+Redfish split, because "the same shape" would
have meant a per-iLO-generation branch and two provenances for one
vendor's servers. That is DRY correctly refused. The general rule: the two
things must have the same *reason* to change, not the same *shape*. Field
mappings that look identical across vendors (`_as_int`, `_psu_health`)
change for different reasons — a Cisco firmware release vs. a DMTF schema
revision — so `ucs_manager/mapping.py:688 _as_int` and
`redfish/mapping.py:114 _as_int` being separate is correct, not debt.

---

## 3. `@dataclass` vs Pydantic

### Measured, at this project's scale

50,000 instances, this venv (`pydantic 2.13.4`, CPython 3.13.12),
8 fields shaped like `ProviderServer`'s hot ones:

| Type | Construct 50k | Peak heap |
|---|---|---|
| `@dataclass(frozen=True, slots=True)` | 259 ms | **5.2 MB** |
| `@dataclass(frozen=True)` (no slots) | 237 ms | 7.6 MB |
| `BaseModel` | 491 ms | **56.8 MB** |

Pydantic is ~1.9× the construction time and **~11× the heap** for the same
data. The heap number is the one that matters: Intersight already holds
its join tables for the length of a run and is the collector with a real
memory ceiling. `frozen=True, slots=True` on `ProviderServer` is measurably
the right call and should not be revisited. (`slots` costs a little
construction time vs. plain frozen and buys 32% of the memory back.)

### The decision rule

**Pydantic wins at a trust boundary** — where bytes of unknown provenance
become objects: HTTP request bodies, Mongo documents, `INVENTORY_*` env
vars. Validation, coercion, `model_dump(mode="json")` and the FastAPI
schema all come from the same declaration. That is exactly where this repo
uses it (`domain/models/*`, `Settings`).

**A dataclass wins for in-process values you constructed yourself.** No
validation is needed because no untrusted input reached it; `ProviderServer`
is built by *our* mapping code from data a provider already parsed. Pydantic
there would validate our own function's output — cost with no information.

**Where Pydantic is wrong:** per-instance at fleet scale (above); as a
domain value object that needs `__hash__`/identity semantics; anywhere the
validation is a re-check of an invariant the constructor already
established.

**Where a dataclass is wrong:** at a boundary. A `@dataclass` accepts
`ProviderServer(cpu_cores="sixty-four")` in silence — the Pydantic docs'
own dataclass page demonstrates precisely this
(`User(name=['not', 'a', 'string'])` printing happily) and shows
`revalidate_instances='always'` as the fix when a stdlib dataclass is
embedded in a model. Never put a raw dataclass where JSON lands.

**Middle rungs worth knowing before adding a dependency:** `attrs` buys
validators and converters over a dataclass but is not installed and nothing
here needs it (rung 5 of the ladder — don't add it). Pydantic's own
`TypeAdapter`/`TypedDict` path is measurably faster than nested `BaseModel`
per its performance docs, and is the escape hatch if a hot path ever needs
validation *and* speed. `model_construct()` skips validation on a
`BaseModel` — the right tool for reading Mongo documents we wrote
ourselves, and the wrong one for anything else.

---

## 4. Composition vs inheritance for a family that genuinely differs

The vendors differ in *kind*, not degree:

| Provider | The thing that breaks a shared base |
|---|---|
| `intersight` | **Signs** every request (`hs2019`); there is no login step at all |
| `ucs_central` | **Fans out** to N domains discovered at runtime, each its own `UcsManagerProvider` (`ucs_central/provider.py:255`) |
| `openmanage` | **Two logins** — OME appliance + a shared iDRAC account — and delegates hardware to a composed `redfish` provider |
| `oneview` | One appliance, `async with` client, plus an optional per-server PSU fan-out |
| `redfish` | N independent BMCs, a credential auth-guard (`_AuthGuard`, `redfish/provider.py:58`) |
| `fake` | No I/O whatsoever; `health_check` is `return None` |

Nothing is shared between those six except *the three members of the port*.
Any base class carrying "connect, then iterate" would be true for two of
them and a lie for four. This is the classic signal that the commonality is
an **interface**, not an **implementation**.

What the repo does instead, and should keep doing:

- **Composition for reuse across vendors**: `openmanage` composes
  `redfish` (`redfish_for` factory, `run_collector.py:122`) rather than
  inheriting it; `ucs_central` composes `UcsManagerProvider` per domain.
- **Decoration for cross-cutting policy**: `_NameFilteredProvider` wraps
  *any* provider. This is the pattern to reach for again for the next
  cross-cutting concern (a staleness counter, a metrics wrapper, a retry
  budget) — it is O(1) code per concern instead of O(vendors).
- **Free functions for vendor mapping**: every `mapping.py` is module-level
  functions, not methods. Nothing to inherit, everything testable without
  constructing a provider.

Inheritance would be right for exactly one thing: a shared *client* base,
if two vendors' HTTP clients converged. They have not (`oneview/client.py`
is session-header based, `intersight/client.py` is signature based,
`redfish/client.py` juggles per-host tokens *and* basic auth), so this is
speculative. Do not build it.

---

## 5. "One function, one reason to change" and the ~700-line mapping module

`ucs_manager/mapping.py` (707 lines) and `redfish/mapping.py` (855) are
already the right shape and this should be said explicitly so the pass does
not "fix" them:

- ~20 small pure functions each (`_disk_capacity_bytes`, `_psu_wattage`,
  `gpus_from_processors`, `psus_from_supplies`, …), plus one assembler
  (`compute_unit_to_provider_server:580`, `system_to_provider_server:735`).
- Every helper is independently testable and independently *readable next
  to the vendor fact it encodes*.

The assembler is long because a `ProviderServer` has ~25 fields. It has
**one** reason to change (the port's field set changed) and each helper has
**one** reason to change (that vendor's API changed). That is the principle
satisfied, not violated. Length is not the metric.

**Splitting the file makes things worse when** the split is by field group
(`cpu.py`, `storage.py`, `power.py`): a vendor's quirks cross those lines
(UCS reports GPU temperature via a sibling MO; a blade's PSUs belong to its
chassis, not the blade), so a per-group split forces cross-imports and
scatters one vendor's hard-won facts across six files. It also makes
"what does the Cisco collector read?" un-answerable by reading one file.

**Splitting is right when** a helper is genuinely shared by two collectors
with the same reason to change — which is what `ucs_common.py` (218 lines)
already is for UCS Manager/Central, and what `redfish/mapping.py` already
is for standalone-Redfish and Dell. Both splits followed a real second
caller. Neither was speculative. Hold that bar.

---

## 6. Async lifecycle, and "this hook does not apply to this vendor"

### 6.1 Where lifecycle lives today

Nothing implements `__aenter__`/`__aexit__` at the *provider* level. The
only async CM is `OneViewClient` (`oneview/client.py:151`), and the pattern
across the family is per-call:

- `intersight/provider.py:552` — `client = self._new_client()` … `finally:
  await client.aclose()`
- `oneview/provider.py:168` — `async with self._client_factory() as client:`
  (and note it deliberately closes the client *before* yielding, so the
  session isn't held open for the consumer's duration)
- `ucs_manager/provider.py:118`/`:192` — `login()` … `finally: logout()`
- `openmanage/provider.py:174` — `async with self._new_client():` in
  `health_check`

`contextlib.AbstractAsyncContextManager` is the stdlib expression of this
contract, and its source (`contextlib.py:41`) shows what you'd inherit:
`__aenter__` returning `self` for free, `__aexit__` abstract, plus a
`__subclasshook__` doing a structural `_check_methods(C, "__aenter__",
"__aexit__")` — i.e. `issubclass` works structurally on it without
`register()`.

**Making `ServerInventoryProvider` an async context manager is the wrong
move**, and this is worth writing down because it looks tidy: `fake` has
nothing to open, `redfish` opens N sessions lazily under a concurrency
bound rather than one up front, and `ucs_central` opens one per discovered
domain. A provider-level `__aenter__` would be a no-op for at least three
of seven — the "lying stub" the brief asks about — and it would move
resource ownership one level away from the code that knows the lifetime.
Per-call `try/finally` inside `list_servers` is correct. Leave it.

### 6.2 The optional-capability question, concretely: `collection_errors`

Four providers expose it (`ucs_central:281`, `intersight:262`,
`openmanage:136`, `redfish:194`); `oneview` and `fake` do not; it is read
twice by `getattr` (`run_collector.py:526`, `openmanage/provider.py:205`).
`run_collector.py:517`'s docstring already argues the case for not putting
it on the port. Four options, ranked by the error they produce:

| Option | Someone forgets → | Verdict |
|---|---|---|
| **Default implementation** on a base (`return ()`) | Silence. A fan-out collector that forgot to record failures reports a clean run. **Worst error of the four** — it is exactly the failure mode `_NameFilteredProvider` forwards the attribute to avoid. | No |
| **`raise NotImplementedError`** in the base | `NotImplementedError` at the end of a 20-minute collection run, after the servers were already ingested. Late, and indistinguishable from a bug. | No |
| **`getattr` capability probe** (status quo) | Silence, but only for a provider that *chose* not to have it. The absence is the answer, not a mistake. Costs: invisible to ty, and duplicated at two call sites. | Acceptable |
| **A separate narrow Protocol** (`ReportsCollectionErrors`, one member) + one `isinstance`-free helper | ty checks the helper; the capability is named and documented; providers that lack it are *statically* known to lack it. | **Superseded — see §9.3** |

The narrow-protocol version keeps the current runtime behaviour exactly
(`getattr` with a default is still the implementation) but gives the
capability a name and a single home. Concretely: keep
`collection_errors_of` as the one reader, delete the second `getattr` in
`openmanage/provider.py:205` in favour of calling it, and declare the
one-member Protocol next to it so the four implementers are annotatable.
That is the smallest change that improves the error, and it is
interface-segregation applied *once*, for a capability that really is
optional — not as a habit.

`@runtime_checkable` + `isinstance` is **not** worth it here: it checks
name presence only (§1.3), it is documented as slow, and `getattr` with a
default already expresses "absent means none" more honestly.

---

## 7. What this means for server_scan

Named, concrete, in priority order. Nothing here is a rewrite.

1. **`backend/app/domain/ports/provider.py:173` — keep `Protocol`, add
   `@abstractmethod`-free strictness where it's free.** See §8 for the full
   argument. If it changes at all, the change is *documentation and one
   annotation*, not a base class.

2. **Preserve `def list_servers(...) -> AsyncIterator[ProviderServer]` as
   non-`async def`** (`provider.py:178`), and add a one-line comment saying
   why, citing the Liskov error in §1.2(e). This is the highest-risk line
   in any refactor of this seam and currently nothing marks it.

3. **Superseded by §9.3 — `collection_errors` becomes a required member
   of the one contract, not a separate Protocol.** (Original wording:
   `tools/run_collector.py:515` `collection_errors_of` — give the
   capability a name.) Add a one-member `Protocol` beside it, and make
   `backend/app/infrastructure/providers/openmanage/provider.py:205` call
   `collection_errors_of` instead of its own `getattr`. One reader, one
   declaration. (§6.2)

4. **`ty check` does not cover `tests/`** (CLAUDE.md convention 7's command
   is `ty check backend/app tools`). The ~10 inline stub providers are
   therefore checked by nobody, and several carry
   `# type: ignore[arg-type]` comments that ty ignores anyway — coded
   suppressions are inert under ty (CLAUDE.md convention 7). Either extend
   ty's scope to `tests` or delete the dead suppressions; leaving both is
   the worst state. Note this is the *only* thing that would make an ABC's
   runtime enforcement redundant, and it is cheaper than an ABC.

5. **`ProviderServer` stays `@dataclass(frozen=True, slots=True)`**
   (`provider.py:79`). The 11× heap measurement in §3 is the justification;
   put the number in the ADR so nobody re-opens it from taste.

6. **Do not make providers async context managers** (§6.1), and do not
   build a shared client base class (§4). Both are the kind of tidiness
   that costs a lie per vendor.

7. **Reach for the decorator, not the base class, for the next
   cross-cutting concern.** Staleness metrics — item 0 of CLAUDE.md's
   not-done list — is a wrapper over `list_servers` in the shape of
   `_NameFilteredProvider`, not a method every provider must add.

---

## 8. Verdict: ABC vs Protocol vs Protocol + mixin

**Recommendation: keep `ServerInventoryProvider` a `typing.Protocol`.
Do not convert it to an ABC. Do not add a mixin.**

### The argument

1. **There is no shared behaviour to inherit** (§4). The six real providers
   differ in authentication kind, fan-out topology, and login count. A base
   class would hold three abstract members and nothing else — a `Protocol`
   spelled with more machinery and a mandatory import from `domain/` into
   every `infrastructure/` module.
2. **ty already catches the failure the ABC is supposed to catch, earlier.**
   §1.2(a)(b) show a missing member, a typo'd name and a wrong signature all
   caught at the call site with a message that names the member. The ABC's
   `TypeError` arrives at construction, at runtime — and §1.2(d) shows ty
   0.0.76 does **not** flag abstract instantiation statically, so the ABC's
   headline benefit is not a CI benefit in this repo at all.
3. **The stub cost is real and recurring.** ~10 inline test doubles across
   `tests/unit/tools/test_run_collector.py` and
   `tests/integration/test_ingest_partial_reads.py:34` are 3–8 lines each
   precisely because conformance is structural. An ABC makes each one
   import and inherit a domain base to test a CLI tool's filtering logic.
4. **`_NameFilteredProvider` is a decorator, not a subclass**
   (`run_collector.py:457`), and sets `provider_type` in `__init__` — which
   §1.2(c) confirms satisfies the Protocol. Under an ABC it must inherit the
   base it wraps, which is the classic decorator/inheritance smell.
5. **The house style is already this**, five times over
   (`repository.py:51`, `credentials.py:64`, `regex_engine.py:37`,
   `ingest.py:89,99`). One nominal port among six structural ones is a
   coin-flip for the next reader; consistency is itself a maintainability
   property.
6. **`Protocol` + mixin is the worst of the three.** PEP 544 forecloses the
   only thing it promises: default bodies "cannot be used if the subtype
   relationship is implicit and only via structural subtyping", and §1.4
   shows a default-bodied member becomes a *required* member for every
   structural implementer — so adding one silently breaks conformance for
   classes that were fine. A mixin also has to be inherited to do anything,
   which reintroduces the coupling the Protocol avoided while keeping the
   Protocol's lack of runtime enforcement. If shared behaviour ever
   genuinely appears, a **free function or a wrapper class** covers it with
   no MRO at all.

### The counter-argument, stated fairly

The strongest case for the ABC is **the eighth provider written by someone
who has not read this note.** They copy a directory, rename it, forget
`provider_type`, and register it in `PROVIDER_FACTORIES`. Under a Protocol,
whether that is caught depends on ty seeing an annotated call site — which
it does today (`_build_provider`'s return type,
`run_collector.py:541`) — and on nobody adding a `# type: ignore` or an
`Any`-typed factory to make an error go away. Under an ABC, it is caught by
`TypeError` at construction no matter what anyone annotates, and the message
names the missing method exactly. That is a genuinely stronger guarantee
against the specific failure mode this pass exists to prevent, and it is not
theoretical: `PROVIDER_FACTORIES` is typed `dict[ManagerType,
Callable[..., ServerInventoryProvider]]` — `Callable[..., X]` erases the
*arguments*, so a factory with wrong parameters is already unchecked there.

Two things blunt it. First, `TestBuildProvider`
(`tests/unit/tools/test_run_collector.py:101`) constructs every provider, so
the ABC's runtime `TypeError` would fire in CI — but so would the missing
`provider_type` assertions already in that class (`:115`, `:133`, `:168`).
The test suite, not the type system, is what actually gates this today, and
that is true under either design. Second, the fix for the residual gap is
cheaper than an ABC: **extend `ty check` to `tests/`** (recommendation 4),
which makes the structural check cover the doubles too, and add a
conformance assertion for each new provider in the same place the
`PROVIDER_FACTORIES` drift guard already lives.

If the team's judgement is that runtime enforcement at construction is worth
the stub cost — a defensible call for a platform whose collectors run
unattended on CronJobs — then the ABC conversion is safe **only** if
`list_servers` stays declared `def … -> AsyncIterator[ProviderServer]`
(§1.2(e)), `provider_type` stays a plain annotated class attribute rather
than an abstract property, and the base gains **no** concrete methods. Under
those three constraints the ABC is a strictly-heavier `Protocol`; the day
someone adds a fourth thing to the base, it stops being one.

---

## 9. The complete contract — answering the four open questions

§1–§8 answered *nominal or structural*. This section answers *what the
contract contains*. The verdict stands (`Protocol`, not ABC), and the
contract widens from 3 members to 6. All probe output below is verbatim
from `uv run ty check` on ty 0.0.76.

### 9.1 The async context manager: a `Protocol` can express it, and ty enforces it twice

**It can be declared**, and there are two exact spellings that matter:

```python
async def __aenter__(self) -> Self: ...

async def __aexit__(
    self,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
    /,                      # <- positional-only. Not optional. See below.
) -> None: ...
```

**ty catches an omission twice over.** A provider missing `__aexit__`,
probed:

```
error[invalid-argument-type]: Argument to function `take` is incorrect
info: type `NoAexit` is not assignable to protocol `P`
info: └── protocol member `__aexit__` is not defined on type `NoAexit`

error[invalid-context-manager]: Object of type `NoAexit` cannot be used with
`async with` because it does not implement `__aexit__`
```

The second diagnostic is the important one: it fires at the `async with`
statement itself, on the *concrete* type, with no protocol annotation
involved. So even a call site that lost its annotation still gets caught.

**The `/` is load-bearing.** Without positional-only markers, the idiomatic
`async def __aexit__(self, *exc: object) -> None` — which is exactly what
`oneview/client.py:165` already writes — is **rejected**:

```
info: └── protocol member `__aexit__` is incompatible
info:     └── parameter `exc_type` is missing
```

With the `/`, the same `*exc: object` implementation passes. Get this wrong
and the new contract rejects the one class in the repo that already
implements the pattern correctly.

**Who guarantees the `async with` wraps every run?** There are exactly two
call sites, and that changes the answer decisively:

- `tools/run_collector.py:874` — `_run_one_manager`, wrapping
  `ingest_service.ingest(provider, …)`
- `tools/run_collector.py:693` — `_dry_run_one_manager`, wrapping the
  `async for ps in provider.list_servers()` loop

Two `async with` statements, both in one file, both inside functions that
already have `try`/`finally` discipline. That is a small enough surface that
"who guarantees it" is answerable by reading one file — which is the goal
the brief states. Note the session must span `ingest()`, not just
`list_servers()`, because `IngestService.ingest` calls `health_check` first
and then iterates (`run_collector.py:883`'s comment explains why the
collector does not call `health_check` itself).

**Is `contextlib.AbstractAsyncContextManager` as a base the pragmatic answer
for this one axis?** No, and the reason is concrete rather than stylistic.
Inheriting it buys one thing — a free `__aenter__` returning `self` —
and costs the nominal coupling §8 argues against. Meanwhile the free
`__aenter__` is *wrong* for five of seven providers: their `__aenter__` has
real work to do (open the httpx client, log in, discover the API version),
so they override it anyway. The two that would use the default (`fake`,
and arguably `ucs_manager`, whose session is per-domain) can write
`return self` in one line. Its `__subclasshook__` does a structural
`_check_methods(C, "__aenter__", "__aexit__")` (`contextlib.py:58`), so
`issubclass` already works structurally without inheriting — which is the
final argument that inheriting it adds nothing here.

**What this actually fixes:** today no provider closes anything at the
provider level. Each `list_servers` opens and closes its own client
(`intersight/provider.py:568` `finally: await client.aclose()`,
`oneview/provider.py:180` `async with self._client_factory()`), and
`health_check` opens a *second*, separate session. Under the contract the
session is owned by the `async with` in `run_collector`, opened once per
run, and closed on every path including an exception mid-iteration — which
is the path that currently leaks, because an async generator abandoned
mid-iteration runs its `finally` only when the event loop finalises it.

### 9.2 The discovery-vs-detail split: do not name it

**Recommendation: leave it unnamed. No `discover()`/`detail()` pair, no
optional method, no second Protocol.**

The argument is that the two providers that "split" do not split on the same
axis, so any single member naming the split is the wrong abstraction —
DRY's failure mode from §2, applied to a live case:

- **OpenManage splits on *provenance***: OME says *who a server is* (name,
  identity, deployment template); the iDRAC says *what it is* (CPU, DIMMs,
  drives, PSUs). Both passes cover the same server set. The split exists
  because two authorities disagree about which is authoritative for which
  field (ADR-0020).
- **UCS Central splits on *topology***: Central enumerates *endpoints*
  (domains), then each domain enumerates its own servers. The first pass
  yields no server data at all. The split exists because the fleet is behind
  N addresses that are only knowable at runtime.

A single `discover()` returning "the things to then fetch in detail" would
return `OmeIdentity` objects in one case and `DomainTarget` objects in the
other — different types, different cardinality relative to the result, one
of them contributing fields to the final `ProviderServer` and the other
contributing none. That is two mechanisms wearing one name, which is
precisely what makes a contract *less* discoverable, not more.

Both are already expressed in the language that fits them: **composition**.
`OpenManageProvider` composes a `redfish` provider
(`run_collector.py:122`'s `redfish_for` factory); `UcsCentralProvider`
composes one `UcsManagerProvider` per discovered domain
(`ucs_central/provider.py:255`). A reader asking "how does Dell work?" reads
one 346-line file and sees the answer. That is the goal met.

What a new vendor actually needs told is not a method name but a sentence,
and it belongs in the contract's module docstring: *"`list_servers` is the
whole collection. If your vendor needs two passes, do both inside it and
compose the second from an existing provider where one fits — see
`openmanage` (provenance split) and `ucs_central` (topology split)."*

### 9.3 `collection_errors` — revised, with the OneView evidence

**The team lead's evidence changes the answer. Retract §6.2's "separate
narrow Protocol"; make it a required member of the one contract.**

The falsification is exact. `collection_errors_of`'s docstring
(`run_collector.py:517`) justifies keeping the capability off the port
because "only a collector that fans out over several endpoints can partially
fail, so requiring the attribute of every provider … would be ceremony for a
single vendor's shape." There are now **four** fan-out collectors with it
(`ucs_central:281`, `intersight:262`, `openmanage:136`, `redfish:194`) and a
**fifth fan-out collector without it** — OneView, which fans out one request
per server for PSUs (`oneview/provider.py:293`), swallows each failure
(`:355` `except Exception: return uri, None`), counts them into a local
`failures`, logs a warning, and returns. It also tallies unreadable
subresources per server (`:382 _count_unreadable`) and surfaces none of it.
`run_collector.py:1010` turns a non-empty `collection_errors` into exit 3
with the comment that reporting a partial run as success "is how a bad
credential on one domain stays invisible for weeks." **OneView cannot reach
exit 3.** It exits 0 having missed an arbitrary fraction of the fleet's
power data — the exact failure the exit code exists to prevent.

**Does a separate Protocol prevent that? No.** This is the key point and it
is why §6.2 was wrong. A separate `ReportsCollectionErrors` Protocol is
*opt-in*: OneView's author would have had to choose to implement it, which
is the same choice they already failed to make. Worse, the read stays
`getattr`-reflective in two places (`run_collector.py:532`,
`openmanage/provider.py:205`), so no annotation exists for ty to check. A
capability nobody is required to declare is checked by nobody.

**A required member on the one contract does prevent it** — for the half of
the problem a type checker can see:

- ty rejects any provider lacking it at both call sites, naming the member
  (probed: `protocol member 'collection_errors' is not defined on type X`);
- both `getattr` calls are deleted, so the capability has exactly one
  spelling;
- the cost to the two providers that genuinely cannot fail partially is one
  line each. That is **not** a lying stub: `fake` performs no I/O, and
  `ucs_manager` collects one domain whose failure *is* the run's failure
  and propagates to `ucs_central`, which records it. `collection_errors ==
  ()` is a true statement for both, not a shrug.

**The declaration form is not free-choice.** Four providers implement this
as a `@property`. Probed: a read-only `@property` does **not** satisfy a
protocol *variable* member —

```
info: └── protocol member `collection_errors` is incompatible
info:     └── the member does not accept writes of type `tuple[str, ...]`
```

— so declaring `collection_errors: tuple[str, ...]` in the contract breaks
all four. Declared as a read-only property in the Protocol body, all four
forms pass: `@property`, plain class attribute, and instance attribute set
in `__init__` (which is what the test stubs and `_NameFilteredProvider`
use). Probed clean.

**What a type checker still cannot see, and the construction that catches
it.** No type-level construction makes "you fanned out over N endpoints and
reported no errors" an error — the bug is a swallowed exception inside a
method body, and no signature describes that. Two things do catch it, and
both are cheap:

1. **A test per fan-out collector**: inject one failing endpoint, assert
   `collection_errors` is non-empty and `list_servers` still yields the
   rest. `tests/unit/tools/test_run_collector.py:259`'s
   `PartiallyFailedProvider` already asserts the *plumbing*; what is missing
   is the same assertion against each real provider. That is the only thing
   that would have caught OneView, and it is ~15 lines per collector.
2. **Fix the swallow itself** — `oneview/provider.py:355` should record the
   URI and the exception into `self._collection_errors` rather than only
   incrementing a counter, and `_count_unreadable`'s tally should be
   summarised into one entry. That is a bug fix, not a design change, and it
   is required regardless of which contract shape wins.

### 9.4 The lying-stub problem: keep hooks off the contract entirely

**Mechanism: the contract contains only members every provider can implement
truthfully. Vendor-specific behaviour is a constructor argument or a
composed collaborator — never an overridable hook.**

Ranked by the error someone gets when they get it wrong:

| Mechanism | The error when it's wrong |
|---|---|
| Hook with a default no-op body (`async def authenticate(self): pass`) | **None.** Silent. The provider that needed to log in and forgot runs and collects nothing. Unacceptable. |
| Hook raising `NotImplementedError` | `NotImplementedError` mid-run, indistinguishable from unfinished code, after partial ingest. |
| Capability flag (`supports_login: bool`) | The flag and the behaviour drift; nothing checks that a `True` flag has a working login. Adds a member and answers nothing. |
| **Member absent from the contract; behaviour composed in** | ty names the missing collaborator at construction, in `PROVIDER_FACTORIES`, before any network call. |

This is not theoretical — it is what the repo already does and should keep
doing. Intersight has no login because its `__aenter__` opens a signing
client and returns; there is no `login()` member to stub out. OME needs two
logins because its factory takes a second `RedfishCredential`
(`run_collector.py:114`) and refuses to start without it, naming
`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` in the message
(`run_collector.py:107`). OneView stays OneView-only for every iLO
generation because there is no per-generation hook to branch on — ADR-0022,
closed to re-litigation, and the contract must not re-open it by offering a
`redfish_pass()` member OneView would have to stub.

The concrete error a new vendor gets from this design, probed:

```
error[invalid-argument-type]: Argument to function `take` is incorrect
info: type `NewVendorProvider` is not assignable to protocol `ServerInventoryProvider`
info: └── protocol member `collection_errors` is not defined on type `NewVendorProvider`
```

…at `PROVIDER_FACTORIES`' declared return type, in CI, naming the member.
Compare the alternative, which is what a defaulted hook produces: exit 0,
a smaller fleet than yesterday, and nobody paged.

---

## 10. The contract

This is the recommended `backend/app/domain/ports/provider.py` seam, member
by member. `ProviderServer` / `ProviderNic` / `ProviderAttachment` are
unchanged (§3: the 11× heap measurement settles the dataclass question).

```python
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Protocol, Self


class ServerInventoryProvider(Protocol):
    """One vendor's collector. Six members; implement all six.

    Lifecycle, in the order `tools/run_collector.py` drives it:

        async with provider:              # __aenter__ / __aexit__
            await provider.health_check()   # called by IngestService.ingest
            async for server in provider.list_servers():
                ...
        provider.collection_errors        # read after the run

    `list_servers` is the whole collection. A vendor needing two passes
    does both inside it — see `openmanage` (OME names the servers, each
    iDRAC measures them) and `ucs_central` (Central names the domains,
    each domain names its servers). Compose an existing provider for the
    second pass where one fits; there is no `discover()` member, because
    those two splits are on different axes and one name for both would
    hide more than it shows.

    There are no vendor-specific hooks here on purpose: a member you
    would have to stub out is a member that belongs in your constructor
    instead. Intersight signs requests rather than logging in, OME takes
    two logins, and neither shows up as a member of this contract.
    """

    provider_type: str
    """`ManagerType.<X>.value`. Stamped onto every `ProviderServer`."""

    async def __aenter__(self) -> Self:
        """Acquire whatever this vendor needs — a session, a signed
        client, nothing at all — and return self."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        """Release it. Must be safe on every path, including an
        exception raised mid-iteration, and must never raise.

        The `/` is required: it is what lets an implementation write the
        idiomatic `async def __aexit__(self, *exc: object) -> None`.
        """
        ...

    async def health_check(self) -> None:
        """Raise if this endpoint is unreachable or rejects the login.

        Called by `IngestService.ingest` as its first step, inside the
        `async with`. Do not open a second session for it.
        """
        ...

    def list_servers(self) -> AsyncIterator[ProviderServer]:
        """Yield every server in scope, streamed.

        Declared `def`, NOT `async def`: an implementation is an async
        generator (`async def` + `yield`), whose type is
        `AsyncGenerator[ProviderServer, None]`. An `async def` stub here
        types as `Coroutine[..., AsyncIterator[...]]` and makes every
        implementation an LSP violation ty rejects by name. Do not
        "fix" this line.
        """
        ...

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """One message per endpoint this run could not read, empty if the
        run saw the whole fleet.

        A non-empty tuple is exit code 3 (PARTIAL) — the difference
        between "a smaller estate" and "a bad credential nobody noticed
        for weeks". Declared as a read-only property so a `@property`, a
        class attribute and an instance attribute all satisfy it.

        Returning `()` from a single-endpoint collector is a true
        statement, not a stub: its endpoint's failure is the run's
        failure and propagates as an exception.
        """
        ...
```

### Who already has what

| Member | Has it | Needs writing |
|---|---|---|
| `provider_type` | all 7 + `_NameFilteredProvider` (`run_collector.py:477`) | — |
| `health_check` | all 7 | — |
| `list_servers` | all 7 (all already `async def` + `yield`, all already compatible with the `def` declaration) | — |
| `__aenter__` / `__aexit__` | **none of the 7.** `OneViewClient` (`oneview/client.py:151`) has the pattern, at the client level | all 7 + the wrapper. `intersight`, `oneview`, `openmanage`, `redfish`, `ucs_central`, `ucs_manager`: move the client's `try/finally`/`async with` up from `list_servers` into the pair. `fake`: `return self` / `return None` — it owns no resource. `_NameFilteredProvider`: delegate to `self._inner`. |
| `collection_errors` | 4, all as `@property`: `ucs_central:281`, `intersight:262`, `openmanage:136`, `redfish:194`; plus `_NameFilteredProvider:481` forwarding | 3. `fake` and `ucs_manager`: `collection_errors: tuple[str, ...] = ()`, one line each. **`oneview`: real work** — record the swallowed per-server PSU failures (`provider.py:355`) and summarise `_count_unreadable`'s tally (`:382`), because today it fans out and can never report exit 3. |

### What this deletes

- `collection_errors_of` (`tools/run_collector.py:515`) and its docstring's
  now-falsified reasoning.
- `getattr(provider, "collection_errors", ())` — `run_collector.py:532`.
- `getattr(redfish, "collection_errors", ())` —
  `openmanage/provider.py:205`.
- The per-`list_servers` client teardown in five providers, which becomes
  the `__aexit__`.

### What still is not type-checked, and must be tested instead

1. That a fan-out provider actually *records* what it swallowed (§9.3) —
   one injected-failure test per fan-out collector.
2. That the `async with` is present at both call sites — but ty's
   `invalid-context-manager` fires the moment someone uses a provider
   without it, so this is checked in practice as long as the annotations
   stay.
3. The ~10 stub providers in `tests/` (§7 recommendation 4): still outside
   `ty check backend/app tools`. Widening ty's scope to `tests` is what
   makes the new 6-member contract check the doubles too, and it is now
   worth more than it was at 3 members — each stub gains two lifecycle
   members and an error tuple, and nothing currently verifies they got them
   right.
