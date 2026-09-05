# ADR-0023: `ServerInventoryProvider` becomes an ABC with a `collect()` template method

Date: 2026-09-06
Status: Accepted

Supersedes nothing. Extends the seam `docs/adr/0009-ucs-manager-collector.md`,
`0014-ucs-central-multi-domain-collector.md`, `0016-redfish-standalone-collector.md`,
`0017-intersight-collector.md`, `0020-dell-identity-from-ome-hardware-from-redfish.md`
and `0022-oneview-only-hpe-collector.md` each implement against. None of
those decisions changed; this is the contract they all sit behind.

## Context

`app.domain.ports.provider.ServerInventoryProvider` was a three-member
`typing.Protocol`:

```python
class ServerInventoryProvider(Protocol):
    provider_type: str
    async def health_check(self) -> None: ...
    def list_servers(self) -> AsyncIterator[ProviderServer]: ...
```

Seven providers implement it (`fake`, `ucs_manager`, `ucs_central`,
`intersight`, `openmanage`, `oneview`, `redfish`), and the real lifecycle
every one of them needs is longer than three members: connect/authenticate,
report partial failures, list servers, disconnect. The Protocol named none
of the rest, so each provider reinvented it — and one didn't.

**The motivating bug.** Four providers each hand-wrote the same four lines:

```python
self._collection_errors: list[str] = []
...
self._collection_errors.append(message)
@property
def collection_errors(self) -> tuple[str, ...]:
    return tuple(self._collection_errors)
```

`oneview` did not. `tools/run_collector.py` read the attribute reflectively
(`collection_errors_of`, `getattr(provider, "collection_errors", ()) or ()`)
specifically so a provider that "cannot partially fail" needed no member at
all — and OneView, which fans out over three bulk calls per appliance and
*can* partially fail (a paginated collection can be truncated past HPE's
documented 256-per-request ceiling on `/rest/server-profiles`), fell through
that gap. It logged `oneview.collection_truncated` at ERROR and tallied
unreadable subresources, but had nothing to report to
`collection_errors_of`, so a truncated run exited **0**. `run_collector.py`'s
own comment on the exit-3 branch says why that matters: "some servers were
written, but this run did not see the whole fleet... reported as success it
is indistinguishable from a healthy run against a smaller estate, which is
how a bad credential on one domain stays invisible for weeks." The same
`getattr` had already been copy-pasted into `openmanage/provider.py`,
reading its inner Redfish sub-provider's errors the same reflective way.

## Decision

`ServerInventoryProvider` is now an `abc.ABC`:

```python
class ServerInventoryProvider(ABC):
    provider_type: str

    def __init__(self) -> None:
        self._collection_errors: list[str] = []

    @property
    def collection_errors(self) -> tuple[str, ...]:
        return tuple(self._collection_errors)

    def _record_error(self, message: str) -> None:
        self._collection_errors.append(message)

    async def collect(self) -> AsyncGenerator[ProviderServer, None]:
        self._collection_errors = []
        async with aclosing(self._list_servers()) as stream:
            async for server in stream:
                yield server

    @abstractmethod
    async def health_check(self) -> None: ...

    @abstractmethod
    def _list_servers(self) -> AsyncGenerator[ProviderServer, None]: ...
```

`collect()` is the one method every caller drives. It is a template
method, not a name change: it resets `collection_errors` for the run and
wraps the subclass's `_list_servers()` in `contextlib.aclosing`, so an
abandoned iteration still closes the underlying generator rather than
deferring session teardown to garbage collection. `collection_errors` and
`_record_error` are inherited, not re-implemented — the fifth author (or
the eighth vendor, a year from now) never writes those four lines at all,
because there is nothing left for them to forget.

### Why an ABC, having first recommended a Protocol

The parallel research pass (`docs/notes/2026-09-research-python-design.md`)
recommended keeping a `Protocol` — a 6-member one, verified `All checks
passed!` under this repo's own ty 0.0.76 against every real implementation
shape. Its reasoning was sound as far as it went: ty already catches a
missing or mistyped member structurally, ty 0.0.76 does not statically
flag instantiating an abstract class (probed — the ABC's benefit is a
runtime `TypeError`, not a CI gate), and PEP 544 makes a Protocol with
default method bodies actively worse (a defaulted member becomes
*required*, breaking previously-conforming classes).

That recommendation answered the wrong question. Every point above is
about **catching a mistake** — nominal versus structural, where the check
fires. But OneView's defect was not a wrong shape that a stricter checker
would have flagged. Nobody forgot to *declare* `collection_errors`;
somebody forgot to *reimplement* four lines of bookkeeping for the fifth
time. A Protocol holds no code: the best it can do is tell that author
they got it wrong, after which they hand-write the same four lines a
fifth time and hopefully get them right this time. An ABC is the only
construct that lets them not write those lines at all — the base class
carries the bookkeeping, and a subclass literally has nothing to
reimplement.

The Protocol research's "~none to inherit" measurement (six providers
differ in kind: Intersight signs requests instead of logging in, UCS
Central fans out to N discovered domains, OpenManage needs two logins,
`fake` does no I/O at all) is accurate about the code as it stood and
answers the wrong question. It measures what *is* shared today, not what
*should* be. The four collection-errors implementations that already
existed, independently, in nearly identical form, are the evidence that
this bookkeeping is exactly the kind of thing that should be inherited
rather than re-derived per vendor.

### What was cut from the first ABC draft, and why

The first draft also added `__aenter__`/`__aexit__` calling abstract
`_connect()`/`_disconnect()` hooks, on the reasoning that "a leaked vendor
session becomes impossible" only if connect/disconnect are structurally
guaranteed. A pre-implementation review (Codex, plus this session's own
reading of every call site) found that **nothing in this codebase ever
writes `async with provider:`** — `ingest.py`, both `run_collector.py`
call sites, `ucs_central._collect_domain` and `openmanage`'s inner
Redfish pass all construct a provider and iterate it directly, or wrap
its generator in `contextlib.aclosing`. Shipping the context-manager pair
would have added two abstract methods every provider must fill in while
guaranteeing nothing, since nothing calls `__aenter__`.

Worse, it would have actively misled two providers:

- `redfish/provider.py`'s `health_check()` deliberately makes **no
  network call** — "probing a canary host would reintroduce the single
  point of failure this collector exists to remove" — so it has no single
  connection to open or close. It holds N per-host clients, not one.
- `ucs_manager/provider.py`, driven only as `ucs_central`'s per-domain
  child via `contextlib.aclosing(provider.collect())`, never has its
  `health_check()` called by that caller at all. A future implementer who
  trusted the ABC's `__aenter__`/`_connect()` promise and moved login
  there would silently stop connecting under the real call pattern.

Also cut: hoisting run-summary logging into the base. The five providers'
own summaries carry genuinely different fields at genuinely different
cardinality — `redfish` logs `hosts_failed` once per run,
`intersight` logs `unreadable_subresources` once per run, `ucs_central`
logs once **per domain**, `oneview` logs inline with no distinct
completion event at all. There is no shared shape to factor out; forcing
one would have produced exactly the "base method with five vendor-specific
optional parameters" this design was chosen to avoid growing into.

### The failure mode accepted, knowingly

Every provider is now coupled to one base class. The day a vendor
genuinely does not fit this lifecycle, the temptation will be to bolt an
optional hook onto the base rather than admit the misfit — watch for the
base class growing optional parameters over time; that is this design's
disease. It is accepted anyway, because it is slower and more visible
than the disease it replaces: five providers quietly diverging on the
same bookkeeping until one of them didn't.

### Three mechanical constraints, verified against this repo's ty 0.0.76

1. **`_list_servers` (and `collect`) must be declared `def`/`async def`
   with an `AsyncGenerator` return annotation carrying no `yield` in the
   abstract stub.** An `async def` stub with no `yield` in its body types
   as `Coroutine[Any, Any, AsyncGenerator[ProviderServer, None]]`, not
   `AsyncGenerator[ProviderServer, None]` — the meaning is decided by
   whether the body contains a `yield`, which a stub cannot have. Every
   concrete override, which genuinely is an async generator, then
   violates that Coroutine-typed abstract signature under Liskov
   substitution, and ty rejects all seven providers at once. `AsyncGenerator`
   rather than the narrower `AsyncIterator` because `collect()` wraps
   `_list_servers()` in `contextlib.aclosing`, which needs `.aclose()` —
   `AsyncIterator` doesn't structurally guarantee it, `AsyncGenerator`
   does.
2. **`_NameFilteredProvider` (`tools/run_collector.py`) is a wrapper, not
   a vendor, and now inherits nominally.** It overrides `_list_servers()`
   (iterating `self._inner.collect()` and filtering by name) and
   overrides the `collection_errors` property to **delegate** to
   `self._inner.collection_errors` rather than accumulate its own — it
   never calls `_record_error`, so inheriting the base's bookkeeping
   unmodified would always read back empty, making every filtered run
   (every real run, since `INVENTORY_COLLECTOR_NAME_PATTERN` defaults to
   `^ocp`) look complete even when the wrapped provider missed part of
   the fleet. Covered by
   `tests/unit/tools/test_run_collector.py::TestNameFilteredProviderCollectionErrors`.
3. **A test double standing in for a provider must genuinely inherit the
   ABC** wherever production code calls `.collect()` on it (as opposed to
   a double whose `collection_errors`/results are read but never driven
   through `collect()` — e.g. `test_run_collector.py`'s
   `PartiallyFailedProvider`, whose only consumer is a faked
   `IngestService` that never touches the provider at all; that one stays
   duck-typed, with `collection_errors` as a plain settable attribute,
   which is the honest double for a scenario where nothing actually
   iterates it).

## What changed per provider

| Provider | Before | After |
|---|---|---|
| `fake` | No `collection_errors`. | Inherits it (always empty — nothing here can partially fail). |
| `ucs_manager` | No `collection_errors`. | Inherits it (single-pass; empty in practice). |
| `ucs_central` | Hand-rolled `_collection_errors`/`collection_errors`/`.clear()`. | Deleted; uses inherited `_record_error`/`collection_errors`; `collect()` resets the list instead of a manual `.clear()` at the top of `list_servers`. |
| `intersight` | Same hand-rolled trio. | Same deletion; `_new_client`/budget-exhaustion/subresource-failure paths call `_record_error`. |
| `redfish` | Same hand-rolled trio, plus a manual `.clear()`. | Same deletion; every `.append(...)` call site becomes `_record_error(...)`. |
| `openmanage` | Hand-rolled trio; read its inner Redfish sub-provider's errors via `getattr(redfish, "collection_errors", ())`. | Same deletion; now calls `redfish.collect()` (not `.list_servers()`) and reads `redfish.collection_errors` directly — guaranteed to exist. |
| `oneview` | **No `collection_errors` at all** — the motivating bug. | Inherits it. `OneViewClient.get_all` now records one message per truncated page (`self.truncations`, naming the path, the reported total and the count fetched) alongside its existing `oneview.collection_truncated` ERROR log; `OneViewProvider._list_servers` reads `client.truncations` after each bulk call and calls `_record_error` for each. Per-server unreadable-subresource and PSU-fetch failures are deliberately **not** wired to `collection_errors` — those are already correctly handled by `psus=None`/`unread_fields` (carry-forward), and are a different failure class from "part of the fleet's server list was never even fetched." |

`collection_errors_of` (`tools/run_collector.py`) is deleted, along with
its `getattr` copy in `openmanage/provider.py:205`. `_run_one_manager`
now reads `provider.collection_errors` directly.

## Consequence: OneView can now exit 3

A truncated OneView run — more profiles/hardware than one page returned,
past HPE's undocumented behaviour on the 256-per-request ceiling — now
produces a non-empty `collection_errors`, which `run_collector.py` turns
into exit code 3 (PARTIAL) instead of 0. **This is new, intentional
behaviour, not a regression**: a CronJob that previously reported success
on a truncated run will now report PARTIAL. The truncation message names
the path, the reported total and the fetched count, so the failed job's
own log line answers "did we hit the paging cap or lose a connection"
without needing to open a second log stream. Per-server unreadable
subresources continue to succeed the run, unchanged — see the `oneview`
row above.

## Verification

Full gate: `uv run pytest -q` (1037 passed), `uv run ruff check .` and
`uv run ruff format --check .` (both clean), `uv run ty check backend/app
tools` (`All checks passed!`). New/changed tests:
`tests/unit/tools/test_run_collector.py::TestNameFilteredProviderCollectionErrors`
(the wrapper-delegation regression the motivating bug needed) and
`tests/unit/infrastructure/providers/test_oneview_provider.py::TestCollectionErrors`
(a truncated page becomes a `collection_errors` entry; a complete run
reports none). Vendor mapping logic in all seven providers is untouched —
this restructures the contract around it, not the mappings.

## Deferred

Extending `ty check` to `tests/` (currently scoped to `backend/app tools`)
is a separate, larger pass: several test doubles carry mypy-style
`# type: ignore[method-assign]`/`[arg-type]` comments that ty does not
honour (it wants a bare `# type: ignore`), so bringing `tests/` under ty
will surface a real batch of pre-existing suppressions that currently
suppress nothing. Left for its own commit rather than folded in here —
see `docs/notes/2026-09-handoff-phase1-followups.md`.
