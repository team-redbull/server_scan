# Decorators: mechanics, `ty` behaviour, and whether this repo wants any

Research note for the production-hardening pass, 2026-09-06. Branch
`dev-refactor`. No source code was changed by this note.

Everything under "Mechanics" marked **[measured]** was run in this
repository's own virtualenv: CPython 3.13.12, `ty` 0.0.76, FastAPI
0.141.1, httpx 0.28.1, prometheus-client 0.26.0. Scratch files live
outside the repo. `ty` is configured (`pyproject.toml`,
`[tool.ty.environment]`) to check against the 3.12 floor.

**Bottom line up front: add one project-authored decorator, or zero.**
Four of the five candidates are DON'T ADD. The fifth (retry) is a real
gap, but the fix is not a decorator. See "Verdicts".

---

## 1. Mechanics

### 1.1 `functools.wraps`: what it fixes and what it does not

`functools.wraps(f)` is `functools.partial(update_wrapper, wrapped=f)`.
`update_wrapper` copies `WRAPPER_ASSIGNMENTS`, updates
`WRAPPER_UPDATES`, and sets `__wrapped__`.

**[measured]** on 3.13.12:

```
WRAPPER_ASSIGNMENTS = ('__module__', '__name__', '__qualname__',
                       '__doc__', '__annotations__', '__type_params__')
WRAPPER_UPDATES     = ('__dict__',)
```

(CPython docs: [`functools.update_wrapper`](https://docs.python.org/3/library/functools.html#functools.update_wrapper).)

What it fixes:

- **Identity in tracebacks and logs.** `__name__`, `__qualname__`,
  `__module__` are the wrapped function's. **[measured]** a wrapper
  around `plain_coro` reports `__module__='__main__'`,
  `__qualname__='plain_coro'`.
- **Docstrings.** `__doc__` is copied — which matters here, because
  convention 8 in `CLAUDE.md` requires a Google-style docstring on every
  function. Without `wraps`, every decorated function's docstring
  silently becomes the wrapper's.
- **`inspect.signature`.** It follows `__wrapped__` by default
  (`follow_wrapped=True`), so the *original* signature is recovered.
  **[measured]**:

  ```
  inspect.signature(wrapper)                       -> () -> 'int'
  inspect.signature(wrapper, follow_wrapped=False) -> (*args, **kwargs) -> 'int'
  ```

  Note the second line: `__annotations__` is copied too, so the raw
  signature already carries a return annotation that does not belong to
  it. This is a real footgun for anything that introspects
  `__annotations__` directly rather than through `signature()`.

What it does **not** fix:

- **The static type.** `wraps` is a runtime operation. A type checker
  sees only the annotations on the decorator. §1.2.
- **`inspect.iscoroutinefunction` / `isasyncgenfunction`.** These read
  the wrapper's own code flags and do **not** unwrap. **[measured]**: a
  sync wrapper around an async function reports
  `inspect.iscoroutinefunction(...) == False` and
  `asyncio.iscoroutinefunction(...) == False`, even though awaiting the
  result still works. Anything dispatching on that check will make the
  wrong choice — FastAPI historically did, §1.5. (3.12+ offers
  [`inspect.markcoroutinefunction`](https://docs.python.org/3/library/inspect.html#inspect.markcoroutinefunction)
  for the deliberate case.)
- **The signature actually accepted at runtime.** `signature()` reports
  the original; the wrapper still accepts anything. If the wrapper adds
  or drops a parameter, `wraps` makes it *lie*.

### 1.2 Typing a decorator: `ParamSpec`, `TypeVar`, `Concatenate`

[PEP 484](https://peps.python.org/pep-0484/) gave `Callable[[int], str]`
but nothing that could say "same parameters as the function I wrapped" —
so pre-612 decorators were typed `Callable[..., Any]`, which erases the
signature. [PEP 612](https://peps.python.org/pep-0612/) added `ParamSpec`
and `Concatenate` for exactly this.

The correct shape, in the PEP 695 syntax this repo already uses
(`app.infrastructure.singleflight` declares `async def coalesce[T]`):

```python
def trace[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return fn(*args, **kwargs)
    return wrapper
```

`Concatenate` is for a decorator that *injects* a leading argument:
`Callable[Concatenate[str, P], R] -> Callable[P, R]` says "takes a
connection first, hands back something that does not".

**`collections.abc.Callable`, not `typing.Callable`.** `typing.Callable`
is a deprecated alias since 3.9
([PEP 585](https://peps.python.org/pep-0585/)); this repo already imports
from `collections.abc` everywhere (`app.infrastructure.singleflight`,
`app.domain.ports.provider`), and ruff's `UP` ruleset — selected in
`pyproject.toml` — enforces it.

#### How well does `ty` 0.0.76 handle each? **[measured]**

| Construct | `ty` 0.0.76 |
|---|---|
| `Callable[..., Any]` decorator | reveals `(...) -> Any`; **every call checks clean**, including `add("nonsense", None, 1, 2, 3)` |
| `ParamSpec("P")` + `TypeVar` | reveals `(a: int, b: int) -> int` — full fidelity |
| PEP 695 `[**P, R]` | identical to the above |
| wrong argument type through the decorator | `error[invalid-argument-type]` |
| missing argument | `error[missing-argument]` |
| wrong assignment of the return | `error[invalid-assignment]` |
| `Concatenate[str, P]` | reveals `(table: str, *, limit: int = 10) -> int`; extra positional caught as `error[too-many-positional-arguments]` |
| decorated **method** | `s.greet` reveals `(who: str) -> str` — `self` bound correctly |
| `@property` over a ParamSpec decorator | `s.label` reveals `int` — correct |
| `Callable[P, Awaitable[R]]` async decorator | reveals `(host: str) -> CoroutineType[Any, Any, int]`, and the repo's enabled `unused-awaitable` rule fires on a forgotten `await` |

So: **ty handles ParamSpec, Concatenate, PEP 695 syntax, bound methods
and property stacking correctly.** There is no typing reason to avoid a
decorator here.

**The one real ty defect found — overload return types collapse.**
**[measured]**:

```
reveal_type(pick)              -> Overload[(x: int) -> int, (x: str) -> str]
reveal_type(pick(1))           -> int
reveal_type(trace(pick)(1))    -> int | str     # <-- precision lost
```

Passing an overloaded function through a `ParamSpec` decorator keeps the
*arity* of each overload but collapses every return type to the union of
all of them. `int` silently becomes `int | str`. This is a known hard
case for `ParamSpec` (PEP 612 does not specify overload propagation),
and it is a **latent** trap rather than a live one: `grep -rn "@overload"
backend/app tools` returns nothing today. It becomes a real bug the day
someone writes an overload and decorates it — the checker gets *less*
precise, silently, with no diagnostic.

### 1.3 Async-aware decorators, and the async-generator break

**`inspect.iscoroutinefunction` branching inside one decorator is a
smell** for three reasons: it makes one name mean two control flows, the
static type of the result is a union of two shapes that no annotation
expresses honestly, and — measured above — the check is defeated by any
intermediate `wraps`-based wrapper. Two decorators (`timed` /
`timed_async`) with two names is the boring, correct answer.

**Async generators are a third shape, not a variant of the second**, and
this matters directly: `ServerInventoryProvider.list_servers`
(`backend/app/domain/ports/provider.py:178`) is declared
`def list_servers(self) -> AsyncIterator[ProviderServer]`, and every
provider implements it as an `async def ... yield` generator.

The naive "async decorator" breaks it in both directions. **[measured]**,
wrapping `list_servers` with `async def wrapper(...): return await fn(...)`:

```
original  isasyncgenfunction: True
wrapped   isasyncgenfunction: False
wrapped   iscoroutinefunction: True

async for item in wrapped():
  TypeError: 'async for' requires an object with __aiter__ method, got coroutine
await wrapped():
  TypeError: object async_generator can't be used in 'await' expression
```

Both failures are `TypeError` at the *call site*, not at decoration —
i.e. at collector runtime, in a CronJob pod, not in CI. The correct
wrapper is itself an async generator:

```python
@functools.wraps(fn)
async def wrapper(*args, **kwargs):
    async for item in fn(*args, **kwargs):
        yield item
```

**[measured]**: `isasyncgenfunction` True, yields `[1, 2]`.

`tools/run_collector.py`'s `_NameFilteredProvider.list_servers`
(line ~493) is already exactly this shape — an `async for`/`yield`
passthrough, not a `return await`. It is the working precedent, and it is
a **class**, not a decorator (§4.3).

### 1.4 Stacking order with `@property`, `@classmethod`, `@staticmethod`, `@abstractmethod`

Decorators apply bottom-up. `property`, `classmethod` and `staticmethod`
return *descriptor objects*, not functions, so a function-shaped
decorator must sit **above** them (applied after) — it must receive the
plain function.

**[measured]** — correct order works, wrong order fails two different
ways:

```
@property           @untyped
@trace         vs   @property
def value(...)      def value(...)

Good().value -> 1                # correct
Bad().value  -> <bound method wrapper of <Bad object>>   # SILENT: returns
                                                         # the method, not 1
```

```
@classmethod        @untyped
@trace         vs   @classmethod
def make(cls)       def make(cls)

Good.make() -> "made"
Bad.make()  -> TypeError: 'classmethod' object is not callable
```

The `classmethod` case is loud. **The `property` case is silent** — the
attribute quietly evaluates to a bound method object, which is truthy,
JSON-unserialisable, and will surface far from its cause.

`@abstractmethod` has the mirror rule and it is the opposite way round:
it must be the **innermost** decorator (listed last), because
`ABCMeta` looks for `__isabstractmethod__` on the final object, and only
`property`/`classmethod`/`staticmethod` propagate that flag outward. A
plain wrapper above `@abstractmethod` swallows it and the ABC stops
enforcing anything. (CPython docs:
[`abc.abstractmethod`](https://docs.python.org/3/library/abc.html#abc.abstractmethod)
— "when `abstractmethod()` is applied in combination with other method
descriptors, it should be applied as the innermost decorator".)

This repo has 20 `@property` and 16 `@classmethod`/`@staticmethod`, so
any project-authored decorator lands in this minefield immediately.

### 1.5 FastAPI: a decorator that drops the signature breaks DI

FastAPI reads the endpoint's signature to build its dependency graph:
`fastapi/dependencies/utils.py:198` `_get_signature` calls
`inspect.signature(call, eval_str=True)`, and `get_typed_signature`
(line 213) turns each parameter into a query/path/body field.

**[measured]** against a live `TestClient` on FastAPI 0.141.1, four
endpoints, each `GET ...?page_size=99`:

| decorator over the endpoint | response | OpenAPI parameters |
|---|---|---|
| `@functools.wraps`, async wrapper | `200 {"page_size":99}` | `['page_size']` |
| **no `functools.wraps`** | **`422` "Field required: query.args"** | **`['args', 'kwargs']`** |
| `wraps`, *sync* wrapper over async endpoint | `200` | `['page_size']` |
| `wraps`, *async* wrapper over sync endpoint | `200` | `['page_size']` |

So the failure mode is exact and total: **omit `functools.wraps` and
FastAPI publishes `args` and `kwargs` as required query parameters** —
every request 422s, and the drift is visible in the OpenAPI document the
frontend reads.

Rows 3 and 4 pass only because FastAPI 0.141's
`_is_coroutine_callable` (`fastapi/dependencies/models.py:196-220`)
explicitly retries its check against `_unwrapped_call(call)`, i.e. it
follows `__wrapped__`. That is a FastAPI implementation detail, not a
language guarantee — `run_endpoint_function`
(`fastapi/routing.py:344`) still branches
`await dependant.call(...)` vs `run_in_threadpool(dependant.call, ...)`
on that boolean, and on a checker that did not unwrap, row 4 would
return an un-awaited coroutine object to the serialiser. Do not rely on
it: match the wrapper's sync/async-ness to the endpoint's.

### 1.6 When a decorator is worse than a plain call

- **Hidden control flow.** A decorator that can retry, swallow, or
  short-circuit makes the call site read as one call when it is N, or
  zero. `@retry` on a non-idempotent call is a data bug that is invisible
  where the call is written.
- **Invisible at the call site.** Reading `await client.get_json(path)`
  tells you nothing about a retry policy declared 300 lines up. An
  explicit `await self._send(...)` loop, which is what
  `redfish/client.py:456` does, keeps the behaviour where the behaviour
  is.
- **Untestable in isolation.** Testing decorated behaviour means either
  building a fake function to decorate (testing the decorator, not the
  code) or reaching through `__wrapped__` (testing the code, not the
  decorator). A plain helper is directly callable from a test.
- **Breaks go-to-definition and stack traces.** Every frame becomes
  `wrapper` in `decorators.py`. `wraps` fixes the *name*, not the frame.
- **Costs a checker's precision.** §1.2's overload collapse is a
  measured example: passing a function through a decorator made `ty`
  *less* able to catch a mistake.

A decorator earns its place when the behaviour is (a) genuinely
cross-cutting across many call sites, (b) uniform — no per-site policy —
and (c) something a reader can safely not think about. Fail any of the
three and a named function called explicitly is the better tool.

---

## 2. Verdicts on the five candidates

### 2.1 Retry/backoff on vendor API calls — **DON'T ADD (a decorator). Fix the gap directly.**

`tenacity` is **not** a dependency; `pyproject.toml`'s runtime list is
fastapi, pydantic, pymongo, redis, httpx, cryptography, structlog,
prometheus-client, regex, uvicorn, ucsmsdk, ucscsdk. Adding one would be
rung 5 of the ladder for something two files already do in ~40 lines.

Where retry actually stands today, per client:

| client | retry today |
|---|---|
| `redfish/client.py` | **yes** — `_send` loop, `_MAX_ATTEMPTS`, `_RETRY_STATUSES = {429, 503}`, `_backoff` honouring `Retry-After` |
| `intersight/client.py` | **yes** — `_RETRY_STATUSES = {429, 500, 502, 503, 504}`, `max_retries` configurable, `_backoff_seconds` honouring `Retry-After` |
| `oneview/client.py` | **no** — `_request_json` catches `httpx.HTTPError` once and raises |
| `openmanage/client.py` | **no** — same shape |
| `ucs_manager` / `ucs_central` | via `ucsmsdk`/`ucscsdk`, wrapped in `asyncio.to_thread` |

The two that exist are not generic, and that is the point. From
`redfish/client.py:459`'s own docstring:

> Never retries a 4xx — a rejected credential retried across an estate is
> what locks accounts — and never retries an `SSLError`, which is a
> configuration problem rather than a transient one.

Intersight retries 5xx; Redfish deliberately does not. Redfish raises a
distinct `RedfishTlsError` from a `ConnectError` whose `__cause__` is an
`ssl.SSLError`. A `@retry(attempts=3)` decorator carries none of that,
and a decorator parameterised enough to carry all of it is a worse
spelling of the loop that is already there.

**Transport-level retry does not substitute.** **[measured]** in
`httpx` 0.28.1: `AsyncHTTPTransport(retries=n)` passes `n` straight to
`httpcore.AsyncConnectionPool`, which uses it only inside
`AsyncHTTPConnection._connect` — the TCP/TLS establishment loop. It never
retries a request that received a response, never a read timeout, and
never a 429 or 503. It covers a strict subset of what the two hand-rolled
loops handle, and none of the throttling case that motivated them
(`intersight/client.py:6`: "the SDK has no retry or backoff for HTTP
429").

**The real finding:** OneView and OpenManage have no retry at all, and
both are aggregator appliances that throttle. If retry is wanted there,
the lazy fix is a `_send`-shaped loop in each client mirroring
`redfish/client.py:456-501`, or — if the duplication grates after the
third one — a plain `async def send_with_retry(send, *, statuses,
max_attempts)` helper taking a callable, not a decorator. That keeps the
policy visible at the call site and testable without a stub function.

### 2.2 Timing/instrumentation — **ALREADY BETTER AS-IS.**

There is no explicit context-manager timing pattern in this repo to
compare against; the pattern in use is **middleware**, and it is one
place, not many:

- `backend/app/main.py:113-127` — `record_metrics`, an
  `@app.middleware("http")` that times every request and writes
  `http_requests_total` / `http_request_duration_seconds`, labelled by
  `request.scope["route"].path` so the cardinality is bounded by the
  route table rather than by the URL space.
- `backend/app/middleware/request_context.py:46-67` — the one structured
  `request.completed` log line with `duration_ms`.
- `backend/app/infrastructure/redis/cache.py` — `cache_operations_total`
  incremented inline on each of the seven outcomes, next to the
  `try`/`except` that decides which outcome it was.

Those are the only four metrics in `app.observability.metrics`. A
per-function `@timed` decorator would add a *second* place instrumentation
lives, with per-function label cardinality nobody has asked for, to
duplicate what one middleware already covers for every route.

And if a function-level timer were ever wanted, **prometheus-client
already ships it** — rung 5, not rung 7. **[measured]** on 0.26.0:
`Histogram.time()`, `Summary.time()`, `Gauge.time()`,
`Counter.count_exceptions()`, `Gauge.track_inprogress()` all exist, and
`Histogram.time`'s own docstring reads "Can be used as a function
decorator or context manager." Writing our own would be re-implementing a
dependency we already have installed.

The cache metrics deserve a specific note: they cannot become a decorator
without loss. `CacheClient.get` distinguishes hit / miss / error *and*
distinguishes a Redis exception from a JSON decode failure, both mapping
to `outcome="error"` but logging different events
(`cache.get_failed` vs `cache.decode_failed`). That classification is the
function's own logic; a decorator can only see "returned" or "raised".

### 2.3 Per-run error capture — **DON'T ADD. It is a contract problem, and there is a live bug behind it.**

`tools/run_collector.py:517` reads the attribute reflectively:

```python
def collection_errors_of(provider: object) -> tuple[str, ...]:
    return tuple(getattr(provider, "collection_errors", ()) or ())
```

with a stated reason: "only a collector that fans out over several
endpoints can partially fail, so requiring the attribute of every
provider — the fake seeder included — would be ceremony for a single
vendor's shape."

**That reason has expired.** Who implements `collection_errors` today:

| provider | `collection_errors` |
|---|---|
| `redfish` | yes (`provider.py:194`) |
| `intersight` | yes (`provider.py:262`) |
| `ucs_central` | yes (`provider.py:281`) |
| `openmanage` | yes (`provider.py:136`) |
| **`oneview`** | **no** |
| `ucs_manager` | no (not reachable as a collector — CronJob-less by design) |
| `fake` | no (correct — a seeder cannot partially fail) |

Four of the five real collectors have it. It is no longer "a single
vendor's shape"; it is the norm, with one omission.

**The omission is a live defect, not a style question.** OneView records
every partial failure as a log line only —
`oneview.subresources_unreadable` (`provider.py:281`),
`oneview.power_supplies_unreadable` (`provider.py:374`),
`oneview.hardware_without_profile` (`provider.py:265`), and and
`oneview.collection_truncated` (`oneview/client.py:357`, the ERROR
`CLAUDE.md` documents for the 256-profile ceiling). None of them reach `collection_errors`. So
`collection_errors_of(oneview_provider)` returns `()`, and
`run_collector.py:1010`'s partial-run branch never fires: **an HPE run
that silently saw half the fleet exits 0 and looks identical to a healthy
run against a smaller estate** — precisely the failure the exit-3 code
was added to prevent ("which is how a bad credential on one domain stays
invisible for weeks", `run_collector.py:1014`).

A decorator cannot fix this. The failures happen *inside* a fan-out, per
sub-resource, several frames below any function a decorator could wrap;
the provider has to choose which of them mean "this run did not see the
whole fleet". Nor is a decorator the right tool for the contract: making
`collection_errors` part of the `ServerInventoryProvider` Protocol
(`backend/app/domain/ports/provider.py:173`) costs one line in the
Protocol, one property on `oneview` and `ucs_manager`, one on `fake`
returning `()`, and lets `collection_errors_of` and its `getattr` be
deleted — a net *reduction*, with the checker enforcing it. That is the
fix. It belongs in the hardening pass; it is not a decorator item.

### 2.4 Cache-aside around Redis reads — **DON'T ADD.**

The degradation contract is the load-bearing part and it is already
solved one layer down, at the right layer.
`backend/app/infrastructure/redis/cache.py` catches
`(RedisError, TimeoutError)` in all three methods and returns
`None`/no-ops, so a Redis failure is *indistinguishable from a miss* to
every caller. Its docstring says exactly why: "without every route
handler needing its own try/except around a cache call." A decorator
could preserve that only by calling the same `CacheClient` — i.e. by
adding a layer over the layer that already does the job.

The decisive argument is that the three call sites are **not the same
shape**, so there is no common thing to factor:

1. `list_servers` (`api/v1/servers.py:173-244`) — read cache; on miss,
   run `coalesce(cache_key, _compute)`, where the cache *write* happens
   **inside** `_compute` so every coalesced waiter shares one write. A
   `@cached` decorator around the whole handler would place the write
   outside the coalescing and defeat `app.infrastructure.singleflight`.
2. `server_facets` (`servers.py:256-283`) — plain read/compute/write,
   different TTL, no coalescing.
3. `get_server` (`servers.py:296-320`) — **two** cache keys. A
   self-maintained `id -> revision` pointer is read first so that
   `server_key(id, revision)` can be built at all, because the cache key
   embeds the document revision to avoid explicit invalidation. Two
   reads, two writes, and a fall-through that must still work when the
   pointer is stale. The module docstring spends a paragraph on why.

A decorator general enough for all three needs a key function, a TTL, a
serializer, a validator model, an optional coalesce flag, and an optional
second-key indirection. That is a framework. Three explicit call sites,
each ~8 lines and readable in place, is the smaller and more honest
answer — and it is what makes the revision-pointer trick legible.

### 2.5 Deprecation markers — **DON'T ADD. Nothing is deprecated. Item dropped.**

`grep -rni "deprecat" backend/app tools` returns six hits and every one
is a comment about somebody *else's* deprecation: Redfish's `Power`
resource and `ProcessorMetrics` fields
(`providers/redfish/provider.py:692,722`, `redfish/mapping.py:418`,
`domain/models/hardware.py:77`), and Motor's deprecation window
(`mongodb/client.py:5`). There is no deprecated function, endpoint,
setting or field of this project's own.

The repo's actual policy is visible in its git history and it is the
opposite of deprecation: `f9ab059 feat!: remove the rule and policy write
endpoints` and `27b20a8 feat!: make rules and policies read-only` deleted
the endpoints outright, using the `!` marker that `CLAUDE.md` convention 9
routes into the release notes' `### Breaking` section. For an air-gapped
platform deployed as a versioned image with a known operator, a
Conventional-Commits `feat!:` reaching the release notes is a *better*
deprecation channel than a `DeprecationWarning` written to a log nobody
greps.

For the record, if that ever changes:
[PEP 702](https://peps.python.org/pep-0702/)'s
`warnings.deprecated` (stdlib `warnings`, 3.13+; `typing_extensions`
below that) is the right tool over a hand-rolled `warnings.warn`, because
it is the only one a type checker can see — it makes deprecation a
*build-time* signal rather than a runtime log line. But it needs a
`typing_extensions` dependency to run on the 3.12 floor
`[tool.ty.environment]` targets, and there is nothing to apply it to.

---

## 3. Bottom line

**Project-authored decorators worth adding: zero.**

Every candidate resolves to either "already solved at a better layer"
(2.2, 2.4), "solved by an installed dependency" (2.2's
`Histogram.time()`), "not a decorator problem" (2.1, 2.3), or "not a
problem" (2.5). The census in the brief — 40 `@dataclass`, 20
`@property`, 15 `@router.*`, 10 `@classmethod`, 6 `@staticmethod`, 4
`@lru_cache`, 4 pydantic validators, 1 `@asynccontextmanager`, and zero
project-authored — is not a gap. It is a codebase using stdlib and
framework decorators where they fit and plain functions everywhere else,
which is the right ratio.

Two supporting observations:

- The repo already handles the two classic decorator traps correctly
  without a decorator. `app.domain.services.regex_engine:53` writes
  `self._compile = lru_cache(maxsize=1024)(self._compile_uncached)`
  per instance rather than `@lru_cache` on a method — which would keep
  `self` alive in a process-wide cache. And `_NameFilteredProvider`
  (`tools/run_collector.py:457`) is the cross-cutting
  wrap-every-provider concern in the codebase, implemented as a class
  because it must also forward `provider_type`, `health_check` and
  `collection_errors` — three things one decorator cannot return.
- What this pass *should* take from the decorator question is the bug it
  surfaced: **OneView's partial failures never reach
  `collection_errors`, so an HPE run that saw half the fleet exits 0**
  (§2.3). Fix that by putting `collection_errors` on the
  `ServerInventoryProvider` Protocol and populating it in the OneView
  provider — which deletes `collection_errors_of`'s `getattr` and makes
  `ty` enforce the contract.

If exactly one decorator ever gets written here, the mechanical rules
that must hold are: `functools.wraps` always (§1.5 — without it FastAPI
publishes `args`/`kwargs` as required query parameters and every request
422s); `[**P, R]` typing, never `Callable[..., Any]` (§1.2 — ty checks
the first perfectly and the second not at all); a separate async-generator
form for anything wrapping `list_servers` (§1.3); above
`@property`/`@classmethod` and below `@abstractmethod` (§1.4); and no
overloaded function underneath it until ty stops collapsing overload
return types (§1.2).
