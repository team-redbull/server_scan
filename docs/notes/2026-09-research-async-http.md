# Async, blocking SDKs, concurrency and the HTTP client — settled on current merit

Research note for the production-hardening pass, 2026-09-06, branch
`dev-refactor`. **No source code was changed.** Everything below is either
cited to a primary source or was measured on this machine against this
repo's own pinned dependencies (Python 3.13.12, httpx 0.28.1, ucsmsdk
0.9.18, ucscsdk 0.9.0.8).

Three findings are *bugs with reproductions*, not style opinions:

1. `app.infrastructure.singleflight.coalesce` — one cancelled waiter takes
   down the leader with `InvalidStateError` and every other waiter with
   `CancelledError` (measured).
2. `..providers.redfish.provider:252` — `asyncio.timeout` wrapped around an
   async generator's yield loop delivers its cancellation into the
   *consumer*, not into the `async with`, so the run-budget path exits the
   collector with an unhandled `CancelledError` instead of the intended
   exit code 3 (measured).
3. `..providers.ucs_central.client:112` — `wait_for(to_thread(...))` cannot
   cancel a `ucscsdk` call that has no socket timeout of its own, so a
   wedged UCS Central leaks a worker thread and makes the CronJob pod sit
   an extra 300 s at exit (mechanism verified from CPython source).

The `verify` posture and the httpx verdict are in §6.

---

## 1. When `async def` earns its keep

### The rule

`async def` is worth its cost when the function can *suspend* — it awaits
I/O, or it is a coroutine the framework requires (an ASGI handler, an
`__aenter__`, a Protocol method that some implementation awaits). It is
cargo cult when the body never yields control, because the caller then pays
a coroutine allocation and a frame for nothing while gaining no
concurrency.

The costs are real and this repo already pays some of them:

- **Function colour.** A sync caller cannot call a coroutine without an
  event loop. Every `async def` in a call chain forces every caller above
  it to be `async` too. This is the reason `IngestService` and the whole
  provider port are async: one `httpx` call at the bottom colours the
  entire pipeline, including the fake seeder, which needs none of it.
- **Stack traces.** An exception raised inside a task spawned by
  `create_task` has a traceback rooted at the task, not at the logical
  caller. `..providers.redfish.provider:249` creates one task per host, so
  a failure inside `_collect_host` shows no path back to `list_servers`.
  This is why per-host `except` blocks that record into
  `collection_errors` (redfish provider:352-374) are doing more work than
  they look like they are.
- **Testability.** Every test of an async function needs `pytest-asyncio`
  and an event loop. The repo already pins it, so the marginal cost is
  small, but a pure-function domain layer that stayed sync would be
  testable with nothing at all.

### CPU-bound work in a coroutine

There is one real instance and it is *correctly* handled: regex matching in
`app.domain.services.regex_engine` is bounded by
`regex_match_timeout_seconds = 0.25` (`config/settings.py:139`) rather than
being moved off the loop. A 250 ms budget per match is a tail-latency
choice, not a blocking-the-loop bug, because the `regex` module releases
nothing during a match — the bound is what keeps a catastrophic pattern
from parking the loop for seconds.

`generate_latest()` in `main.py:130` is CPU work in a coroutine and is
fine: prometheus-client's text rendering of this registry is microseconds.

### Audited: `async def` that never awaits

The AST sweep over `backend/app` and `tools` (skipping nested defs) found
21 hits. Nineteen are correct and should be left alone:

| Kind | Examples | Verdict |
|---|---|---|
| ASGI/Starlette contract | `main.py:130`, `exception_handlers.py:59,77,96,120`, `api/health.py:33`, `api/v1/health_policies.py:141` | **Keep async.** Starlette awaits exception handlers. For a route, `async def` keeps a trivially-cheap handler *on the loop*; a `def` handler is dispatched to the threadpool, which is strictly worse for a function that returns a constant. |
| `Protocol` / ABC stubs with `...` bodies | `domain/ports/repository.py:52,60,62,79,81`, `domain/ports/provider.py:176`, `application/services/ingest.py:96,100` | **Keep async.** They declare a contract implementations await. |
| Context-manager protocol | `..intersight/client.py:175` (`__aenter__`) | **Keep async.** Required by `async with`. |
| Deliberate no-network health check | `..redfish/provider.py:208`, `..fake/provider.py:60` | **Keep async.** They satisfy `ServerInventoryProvider.health_check`, which other providers implement with real I/O. |

Two are genuinely pointless and one is borderline — see the table in §7.

---

## 2. Blocking SDKs on the event loop

`ucsmsdk` and `ucscsdk` are synchronous. Calling them directly from a
coroutine would park the single event-loop thread for the whole XML round
trip, which in this collector means every other domain's work stops. Both
clients correctly hand them to a thread. The question is *which* mechanism.

### The three options, compared on what actually differs

|  | `asyncio.to_thread` | `loop.run_in_executor(pool, …)` | `anyio.to_thread.run_sync` |
|---|---|---|---|
| Thread pool | The loop's **default** executor, shared process-wide | Yours, explicit | AnyIO's worker pool, bounded by a `CapacityLimiter` |
| Default cap | `min(32, os.cpu_count() + 4)` — **20 on this 16-core box**, and in a pod `os.cpu_count()` reports the *node's* cores, not the CPU limit | whatever you pass | **40**, adjustable via `to_thread.current_default_thread_limiter().total_tokens` |
| Backpressure | None. Excess calls queue unboundedly in the executor's work queue | None, same queue | The limiter blocks the *caller* before a thread is claimed |
| Cancelling a **running** call | **Impossible** | **Impossible** | **Impossible** — except via cooperative `from_thread.check_cancelled()` |
| On cancel | The `await` returns; the thread runs on, slot still held | same | `abandon_on_cancel=False` (the default) *waits* for the thread; `True` abandons it |
| contextvars | Copied — `to_thread` does `ctx = contextvars.copy_context()` then `ctx.run(func, …)` | **Not copied** unless you do it yourself | Copied |
| Dependency | stdlib | stdlib | already installed (transitively, via `httpx`→`anyio`) but not a declared dependency |

Sources: `asyncio.to_thread` source as installed (`ctx = contextvars.copy_context(); … loop.run_in_executor(None, func_call)`);
`concurrent.futures.ThreadPoolExecutor.__init__` default `max_workers`;
AnyIO 4.11 `to_thread.run_sync` signature and its docstring
("`abandon_on_cancel` … `True` to abandon the thread (leaving it to run
unchecked on own) … `False` to ignore cancellations in the host task until
the operation has completed"); AnyIO `docs/threads.rst` ("The default
AnyIO worker thread limiter is set to 40 … this AnyIO-specific limiter
does not impact the default thread pool executor used by asyncio").

### The consequence that matters here: a timeout is not a cancellation

`concurrent.futures.Future.cancel()` returns `False` once the call is
running; there is no mechanism in CPython to interrupt a thread blocked in
`recv()`. So `asyncio.wait_for(asyncio.to_thread(f), t)` bounds **how long
you wait**, never **how long `f` runs**. The thread and its executor slot
are held until the underlying socket gives up on its own.

That is exactly the shape at `..providers/ucs_central/client.py:112`:

```python
return await asyncio.wait_for(
    asyncio.to_thread(func, *args), timeout=self._timeout_seconds
)
```

and the SDK underneath has no timeout of its own. Verified by signature:

```
UcsHandle:  (self, ip, username, password, port=None, secure=None, proxy=None, timeout=None)
UcscHandle: (self, ip, username, password, port=443, proxy=None)
```

`UcsManagerClient` passes `timeout=timeout_seconds` to `UcsHandle`, so a
per-domain call is bounded at the socket. **`UcscHandle` has no `timeout`
parameter at all** — the module docstring says so ("ucscsdk has no timeout
of its own; this deadline is imposed by the collector") and the signature
confirms it. A UCS Central whose TCP connection wedges therefore parks a
worker thread *for the life of the process*.

Two follow-on effects, both concrete:

- Up to four such threads (login, `computeSystem`, `lsServer`, logout) out
  of the ~20-slot default executor. Not fatal on its own, but it is shared
  with every `UcsManagerClient` call for every domain.
- **The pod hangs 300 s at exit.** `asyncio.run` → `Runner.close()` →
  `loop.shutdown_default_executor(constants.THREAD_JOIN_TIMEOUT)`, and
  `THREAD_JOIN_TIMEOUT` is `300` (read from the installed stdlib). After
  the run has logged its summary and computed its exit code, the process
  sits for five minutes and then emits `RuntimeWarning: The executor did
  not finishing joining its threads within 300 seconds.`

**Recommendation.** Keep `asyncio.to_thread` — switching to `anyio` buys a
better *default* cap and `check_cancelled()`, neither of which the SDKs can
use (they never poll). Fix the actual problem instead, which is that the
deadline is imposed at the wrong layer: `ucscsdk`'s handle exposes its
`requests`/`urllib` driver, so the socket timeout belongs there. Failing
that, an explicit `ThreadPoolExecutor` owned by the client and shut down
with `wait=False` at the end of the run removes the 300 s hang without
pretending the call was cancelled.

`asyncio.to_thread`'s contextvars copying is load-bearing here and is worth
not losing: `structlog`'s contextvars binding (request id, manager id) is
what makes a log line emitted from inside a worker thread carry the run's
context. `loop.run_in_executor` does **not** copy the context, so a
hand-rolled executor must wrap the call in `contextvars.copy_context().run`
the way `to_thread` does.

---

## 3. Fan-out: `gather` vs `TaskGroup` vs bounded `Semaphore`

### Failure semantics

- **`asyncio.gather(*aws)`** — default `return_exceptions=False`: the first
  exception propagates *immediately* to the awaiter, and the other
  awaitables **keep running**, unawaited. Their exceptions are then never
  retrieved, which is the "Task exception was never retrieved" warning at
  GC. With `return_exceptions=True` nothing propagates; results and
  exception objects come back positionally, in submission order.
- **`asyncio.TaskGroup`** (3.11+, PEP 654) — the first failing child
  triggers cancellation of **every sibling**, and `__aexit__` raises an
  `ExceptionGroup` of everything that failed. This is structured
  concurrency: nothing outlives the block. It is the right default for
  "these N things are one operation and a failure invalidates the rest".
- **Bounded `asyncio.Semaphore`** — orthogonal. It bounds concurrency, not
  failure handling, and composes with either of the above.

### Which is right for "collect N servers; one failure must not abort the run but must be recorded"

**Not `TaskGroup`.** Its entire value proposition — cancel the siblings on
first failure — is precisely the behaviour this requirement forbids. Using
it would mean catching everything inside each child so no exception ever
reaches the group, at which point the group is doing nothing.

**Not bare `gather` either**, for two reasons that both bite at 10 000
servers: it does not yield results until *all* of them are done, so a run
killed at `activeDeadlineSeconds` persists nothing; and it materialises
every result in memory before the consumer sees one.

**The right shape is what this repo already uses in the two collectors that
matter** — `create_task` per unit, a `Semaphore` acquired *inside* the
task, `as_completed` to stream results as they land, and a per-unit
`try/except` that records into `collection_errors` and returns an empty
list. `..providers/redfish/provider.py:246-281` and
`..providers/ucs_central/provider.py:502-518` are both this shape, and the
comment at redfish provider:340-342 ("Acquired *before* the budget starts,
so time spent queueing for a slot is not charged against the host") records
a subtlety worth keeping: **acquire the semaphore outside the per-unit
deadline, or every unit past the first few times out without a packet
sent.**

`gather(*tasks, return_exceptions=True)` still has one correct use in that
shape, and it is used correctly at redfish provider:280: draining cancelled
tasks in a `finally` so each one runs its session teardown.

The one fan-out that is **not** this shape is
`..providers/oneview/provider.py:365`:

```python
for uri, rows in await asyncio.gather(*(fetch(uri) for uri in to_fetch)):
```

It survives only because `fetch` swallows `except Exception` internally
(line 356). Anything raised *outside* that `try` — the `body.get("data")`
lines at 358-363 — propagates and kills the whole PSU pass, and with it the
appliance's run. And because it is a `gather`, no PSU row reaches the
caller until every one of them has. At the ~2500-call scale the setting's
own comment describes, that is the whole point of streaming lost.

### Citations

- CPython `asyncio` docs, "Running Tasks Concurrently": `gather` —
  "If `return_exceptions` is `False` (default), the first raised exception
  is immediately propagated to the task that awaits on `gather()`. Other
  awaitables in the `aws` sequence **won't be cancelled** and will continue
  to run."
- CPython `asyncio` docs, "Task Groups": "If any task in the group fails
  with an exception other than `asyncio.CancelledError`, the remaining
  tasks in the group are cancelled. … the exceptions are combined in an
  `ExceptionGroup` or `BaseExceptionGroup`."
- PEP 654, "Exception Groups and except\*" — motivation §"Programming
  with…": the ExceptionGroup design exists specifically so a nursery/task
  group can report *multiple* concurrent failures rather than picking one.
- PEP 3156 (the original asyncio PEP) for the coroutine/Future model
  underneath all three.

---

## 4. Cancellation and timeouts

### `asyncio.timeout` vs `wait_for`

`asyncio.timeout(d)` (3.11+) is a context manager that cancels **the
current task** when the deadline passes, then converts the resulting
`CancelledError` into `TimeoutError` in `__aexit__`. `wait_for(aw, t)`
wraps *one awaitable*, cancels that awaitable, and raises `TimeoutError`.
Prefer `asyncio.timeout` for a block of several awaits; `wait_for` remains
right for bounding exactly one.

The critical property, and the source of finding #2: **`asyncio.timeout`
delivers its cancellation to the task, at whatever `await` the task is
currently suspended on.** If the `async with` block belongs to a *suspended
async generator frame*, the task is not in that frame — it is in the
consumer.

### Finding #2, measured

`..providers/redfish/provider.py:252` wraps the streaming loop:

```python
async with asyncio.timeout(self._run_budget):
    for finished in asyncio.as_completed(tasks):
        for provider_server in await finished:
            yield provider_server
```

and the consumer is `IngestService.ingest`, which awaits a Mongo write per
yielded server. Reduced repro (run with `uv run python`):

```python
import asyncio

async def producer():
    try:
        async with asyncio.timeout(0.2):
            for i in range(10):
                await asyncio.sleep(0.01)
                yield i
    except TimeoutError:
        print("PRODUCER: converted to TimeoutError")
    finally:
        print("PRODUCER: finally")

async def main():
    got = []
    try:
        async for v in producer():
            got.append(v)
            await asyncio.sleep(0.05)      # the consumer's Mongo write
    except asyncio.CancelledError:
        print("CONSUMER: saw CancelledError, got", got); raise

try:
    asyncio.run(main())
except asyncio.CancelledError:
    print("TOP LEVEL: run() raised CancelledError")
```

Measured output:

```
CONSUMER: saw CancelledError, got [0, 1, 2, 3]
PRODUCER: converted to TimeoutError (the code's own handler ran)
PRODUCER: finally
TOP LEVEL: run() raised CancelledError -- the timeout escaped as cancellation
```

So the provider's own `except TimeoutError` handler *does* run (during
`aclose`), meaning `redfish.run_budget_exceeded` is logged and
`collection_errors` is appended — but by then the consumer has already been
cancelled. `IngestService`'s `except Exception` (ingest.py:316) does not
catch `CancelledError`, `tools/run_collector.py:896` and `:964` do not
either, and `main()` at run_collector.py:1032 never reaches its
`raise SystemExit(exit_code)`. **The run-budget path — the one designed to
"trip before `activeDeadlineSeconds` and report what it collected" — exits
the pod with an unhandled `CancelledError` traceback and status 1, not the
status 3 that means "partial run".** Everything already ingested is
persisted, so this is a reporting and exit-code failure, not data loss.

**Fix, verified:** `asyncio.as_completed` takes its own `timeout=`, and it
raises `TimeoutError` from the `await finished` *inside the generator
frame*, where the existing handler catches it.

```python
for finished in asyncio.as_completed(tasks, timeout=self._run_budget):
    for provider_server in await finished:
        yield provider_server
```

Measured with the same harness: `PRODUCER: run budget expired, unfinished = 6`
/ `CONSUMER: clean finish, got [0, 1, 2, 3]` / `TOP LEVEL: returned
normally`, exit 0. Partial results preserved, existing `finally` drain
unchanged.

Note the deadline semantics shift slightly and correctly: `as_completed`'s
timeout is wall clock from the call, which is what a "run budget" means.

The *other* two `asyncio.timeout` uses are sound: redfish provider:350
wraps a plain coroutine (`_collect_systems`) inside a task with no yield,
and `..intersight/provider.py` deliberately uses a **polled**
`_over_budget(started)` at phase boundaries instead of a timeout at all —
which is the more conservative choice and is worth keeping. Its cost is a
bounded overshoot of one page request (≤ 60 s read timeout).

### Shielding

`..providers/redfish/client.py:249` is the one place shielding is used and
it is correct and non-obvious:

```python
await asyncio.shield(asyncio.wait_for(self._logout(), timeout=10.0))
```

Without the shield, a cancelled task's `await` on the logout raises
immediately, the session-DELETE is never sent, and the leaked session
counts against a BMC cap often as low as 16. The `wait_for` inside the
shield is what keeps the shield from being unbounded. This is the pattern
to copy for any other must-run teardown.

`OmeClient.logout` (openmanage/client.py:151-165) and
`OneViewClient.logout` have the same session-cap exposure — OneView's own
docstring notes 2400 sessions per appliance and 960 per source IP, with a
24-hour idle life — but neither shields. A cancelled run therefore leaks a
session on both.

### `CancelledError` must-reraise discipline

Since 3.8 `CancelledError` derives from `BaseException`, so a plain
`except Exception` does **not** swallow it. That means every
`except Exception` in this repo is safe *from that specific failure*, which
is worth stating because it is the usual reason this rule is raised. The
audit found **no** bare `except:` and no `except BaseException` that
swallows.

The two `except BaseException` sites both re-raise, correctly:
`..redfish/client.py:228` (close the transport, then `raise`) and
`singleflight.py:59` (set the exception on the shared future, then
`raise`). The second has a different problem — §5.

The subtler discipline point: `except Exception` **does** catch
`TimeoutError`, because `asyncio.TimeoutError` has been an alias of the
builtin `TimeoutError` since 3.11. So a deadline raised inside a block
guarded by `except Exception` is silently converted into an ordinary error.
`IngestService`'s `except Exception` at ingest.py:316 wraps only
`_ingest_one`, and every collector-level deadline is raised from the
generator side of the `async for`, so this does not currently bite — but it
is one refactor away from doing so.

---

## 5. `singleflight.coalesce` — a cancelled waiter kills the leader

`app.infrastructure.singleflight` shares one `asyncio.Future` per key
between the leader and every concurrent waiter, and the waiter branch is a
bare `await existing` (line 53).

**`await`ing a bare `Future` makes it the task's `_fut_waiter`, so
cancelling the task cancels the shared Future.** The leader's later
`future.set_result(result)` (line 69) then raises `InvalidStateError`, and
every other waiter gets `CancelledError`.

Measured against the real module:

```python
leader = asyncio.create_task(coalesce("k", compute))
await asyncio.sleep(0)          # leader registers the future
w1 = asyncio.create_task(coalesce("k", compute))
w2 = asyncio.create_task(coalesce("k", compute))
await asyncio.sleep(0.05)       # both waiters now await the shared future
w1.cancel()                     # ONE client disconnects
```

```
leader -> RAISED InvalidStateError invalid state
w1     -> RAISED CancelledError
w2     -> RAISED CancelledError
```

One disconnecting browser tab turns a coalesced `GET /api/v1/servers` into
a 500 for the leader *and* for every other tab waiting on the same key —
under exactly the concurrent-identical-request load the module exists to
handle, and on the one endpoint (`api/v1/servers.py:248`) that uses it.
Starlette cancels the request task on client disconnect, and a dashboard
polling a saved filter is the described workload, so this is reachable in
production rather than theoretical.

**Fix, verified:** shield the waiter, and make the leader's completion
idempotent.

```python
if existing is not None:
    return await asyncio.shield(existing)   # ty: ignore[invalid-return-type]
...
    if not future.done():
        future.set_result(result)
```

Same harness after the change: `leader -> value`, `w1 -> CancelledError`
(only the cancelled waiter), `w2 -> value`.

A leader that is itself cancelled still cancels everyone. That is
defensible and should be left alone rather than growing a
leader-hand-off — but it should be *said* in the docstring, since the
module's current text promises deduplication without mentioning that the
leader's fate is shared.

---

## 6. `httpx` vs `requests` — and an audit of the four clients

### Verdict: `httpx`, confirmed, and there is nothing to migrate

`requests` is not a dependency of this project and must not become one.
The reasoning is not precedent, it is the two properties this codebase
needs and `requests` cannot supply:

1. **`requests` has no async API and will not get one.** Its documentation
   and issue history are consistent that async support is out of scope.
   Every collector here fans out across a fleet — 16 concurrent BMCs
   (`redfish_fleet_concurrency`), 4 concurrent UCS domains, 8 concurrent
   OneView PSU calls — and doing that on `requests` means a thread per
   in-flight request. The Intersight collector's whole cost model (≈120
   requests for 10 000 servers, joined in memory) assumes an async client.
2. **`requests` has no per-phase timeout.** `requests` offers
   `timeout=(connect, read)` only. httpx separates **connect / read /
   write / pool**, and the pool timeout is the one that matters at fan-out:
   without it, a request that cannot get a connection waits forever rather
   than failing. This repo's own Redfish comment ("connect bounds *is this
   host there at all* … read bounds a BMC that answered but is slow") is
   the argument for the split, and it needs four, not two.

Everything else — `verify` accepting an `ssl.SSLContext`, injectable
`transport` for tests (used by `IntersightClient` and `OneViewClient`),
explicit `Limits` — falls out of the same choice. httpx is pinned at
0.28.1; there is no reason to move.

One thing httpx does **not** give you, and it matters below: **no retries
above the transport layer.** `httpx.HTTPTransport(retries=n)` retries only
connection-establishment failures, never a response status. Any 429/503
policy must be hand-rolled — which two of the four clients have done and
two have not.

### Per-client construction audit

Defaults, read from the installed httpx 0.28.1:
`Timeout(timeout=5.0)` (all four phases), `Limits(max_connections=100,
max_keepalive_connections=20, keepalive_expiry=5.0)`,
`follow_redirects=False`, `http2=False`, `verify=True`.

| Client | `limits` | `timeout` (all four?) | Retry/backoff | `verify` | Redirects | HTTP/2 |
|---|---|---|---|---|---|---|
| `..redfish/client.py:187` | **explicit** `max_connections=1, max_keepalive_connections=0` — deliberate, matches the `Connection: close` header | **explicit, all four**, connect/read split, `write=read`, `pool=connect` | 3 attempts, jittered backoff, honours `Retry-After`, **never** retries 4xx or `SSLError` | `build_ssl_context(target)` — verify on by default, per-host opt-out with a recorded reason, warned every run | `follow_redirects=False`, explicit, with a comment on why | off |
| `..intersight/client.py:163` | **default** (100/20) | `Timeout(read, connect=connect)` → read/write/pool all take `read_timeout`; four are set, though write/pool inherit by accident rather than by decision | 4 retries on {429,500,502,503,504}, full-jitter exponential, honours `Retry-After`, caps at 60 s | **`verify=False`, unconditional** | `follow_redirects=False`, explicit | off |
| `..oneview/client.py:129` | **default** (100/20) | scalar `timeout_seconds` → all four equal | **none** | `verify=verify_tls`, **default `False`** | default `False` | off |
| `..openmanage/client.py:92` | **default** (100/20) | scalar `timeout_seconds` → all four equal | **none** | `verify=verify_tls`, **default `False`** | default `False` | off |

Notes on the columns:

- **`limits`.** All three non-Redfish clients talk to exactly one appliance,
  so a 100-connection pool is a ceiling nothing approaches — except
  OneView's PSU sweep, which fans out at `oneview_psu_concurrency = 8`.
  Harmless today; it becomes a real question if that setting is ever raised
  past 20, at which point keepalive churn starts costing TLS handshakes.
  Setting `Limits(max_connections=psu_concurrency + 2,
  max_keepalive_connections=psu_concurrency + 2)` would make the pool
  describe the workload instead of accidentally exceeding it.
- **`timeout`.** No client is left on httpx's 5 s default, which is the
  important thing. Only Redfish makes a *decision* per phase. For the
  scalar cases, a single value applied to `pool` is generous but not wrong.
  For Intersight, `read_timeout = 60.0` also becomes the write and pool
  timeout, which is fine by luck rather than by intent; making it
  `httpx.Timeout(connect=connect, read=read, write=connect, pool=connect)`
  would say what is meant.
- **Retries.** The two clients with no retry policy are the two that have
  never run against live hardware. OneView's three bulk calls are the whole
  run: a single transient 503 from a busy appliance during `GET
  /rest/server-hardware?expand=all` aborts the appliance's entire
  collection with no partial result. OME's two enumeration calls are the
  same. This is the largest robustness gap after the TLS one, and the
  policy is already written twice in this repo (Intersight's
  `_backoff_seconds`, Redfish's `_backoff`) — it should be extracted, not
  written a third time.
- **Idempotency.** Every retried request in both existing policies is a
  `GET`, and Redfish additionally refuses to retry any 4xx (the comment
  gives the reason: retrying a rejected credential across an estate locks
  accounts). A `POST /SessionService/Sessions` login is *not* retried
  anywhere. That is the correct line and any extracted helper must keep it:
  **retry idempotent reads only.**
- **HTTP/2.** Correctly off everywhere. It would require the `h2` extra —
  a new air-gapped mirror dependency — and buys nothing: BMCs and vendor
  appliances are HTTP/1.1, and Redfish deliberately wants one connection
  with `Connection: close`.

### TLS posture — the finding

The correct posture for an air-gapped estate is the one
`..providers/redfish` already implements, and it should be the model for
the rest:

- verification **on by default**;
- a CA bundle setting (`redfish_ca_bundle`) so an internal CA is the
  scalable answer;
- a **per-host** opt-out that requires a recorded reason
  (`verify_tls_reason`), is logged at WARNING on every run with the
  consequence spelled out ("This BMC's password is sent to whatever answers
  at this address"), and is covered by a test
  (`test_verification_is_on_unless_a_host_opts_out`);
- a TLS floor (`redfish_tls_min_version`, default `TLSv1_2`).

Measured against that, the other three:

1. **`IntersightClient` is a blanket `verify=False` with no override at
   all** (`intersight/client.py:166`). `settings.py` states this
   deliberately: "Deliberately no `intersight_ca_bundle` /
   `intersight_tls_verify` setting … There is no environment variable that
   changes this." The class docstring is admirably honest about the
   consequence ("indistinguishable from a man-in-the-middle, in every
   environment including a real production tenant"). It is documented as
   an explicit user decision, so it is recorded here as a **finding, not a
   defect to fix unasked** — but it should be re-put to the user, because
   its blast radius is the worst of the four: the request carries an RSA
   signature over a `Host` header that nothing verifies belongs to the
   endpoint, and an on-prem Intersight appliance is exactly the kind of box
   that *can* be issued a certificate from the estate's internal CA. The
   minimal change that preserves the decision is to keep `False` as the
   default and add `INVENTORY_INTERSIGHT_CA_BUNDLE`, so the estate that
   *does* have a CA is not locked out of using it.
2. **OneView and OME default to `verify=False`** but do at least take the
   flag from settings, so an estate with a CA can turn them on. What is
   missing is the part that makes Redfish's opt-out safe: nothing logs a
   warning when verification is off, there is no `oneview_ca_bundle` /
   `ome_ca_bundle` (so "turn it on where a trusted chain exists" means
   installing the CA into the *image's* trust store), and there is no TLS
   floor. `verify=False` in httpx disables hostname checking **and**
   certificate-chain checking, and it does not disable anything else — the
   handshake still happens, so a floor is still meaningful.
3. Note for whoever changes any of this: **ruff's `S501` only matches a
   literal `verify=False` keyword.** `verify=verify_tls` and
   `build_ssl_context(...)` are both invisible to it. The comment at
   `redfish/client.py:111` already records this; it is the reason the
   Redfish opt-out has a dedicated test instead of relying on the linter.

### Connection-pool lifetime

- **API process:** one `AsyncMongoClient` and one Redis pool per process,
  created in `lifespan` and closed on shutdown (`mongodb/client.py`,
  `redis/client.py`). Correct. No `httpx` client exists in the API at all —
  the API never talks to a vendor, which is the architecture working.
- **Collectors:** one client per appliance per run, inside an
  `async with`, in a process that runs once and exits. This is the right
  granularity: a per-call client would pay a TLS handshake per request, and
  a per-process singleton has nothing to be shared with.
- **Redfish is per-host by design**, with `max_connections=1` and zero
  keepalive so the `Connection: close` header is honoured rather than
  merely requested. Keepalive expiry is irrelevant there.
- **What a short-lived CronJob process does to a pool:** nothing, provided
  `aclose()` runs. It does, via `__aexit__` on all four. The failure mode
  to watch is not the pool but the *server-side session* — see §4 on
  shielding: OME and OneView both leak an appliance session if the run is
  cancelled, and OneView's cap is per-source-IP.
- **`OmeStandaloneProvider` opens two separate OME sessions per run** —
  one in `health_check` (`_new_client()` as a context manager, immediately
  exited) and one in `_discover`. Two logins and two TLS handshakes where
  one would do. Minor, but `IngestService.ingest` calls `health_check`
  unconditionally, so it is every run.
- `tools/loadtest.py:119` uses the default `Limits` (100/20) while driving
  its own `Semaphore(concurrency)`. If `--concurrency` is set above 100 the
  tool measures its own pool, not the server. Dev tooling; worth a note in
  the script rather than a fix.

---

## 7. Per-file audit table

Severity: **H** = wrong behaviour reachable in production; **M** = a real
robustness or security gap; **L** = clarity or cost.

| # | File:line | Problem | Fix | Risk if unfixed | Sev |
|---|---|---|---|---|---|
| 1 | `backend/app/infrastructure/singleflight.py:53,69` | Waiter does a bare `await existing` on a shared `Future`; cancelling one waiter cancels the Future, so the leader's `set_result` raises `InvalidStateError` and every other waiter gets `CancelledError`. Measured. | `return await asyncio.shield(existing)`; guard `set_result`/`set_exception` with `if not future.done()`. | One disconnecting client turns a coalesced `GET /api/v1/servers` into a 500 for every concurrent caller of the same key — under exactly the load the module exists for. | **H** |
| 2 | `backend/app/infrastructure/providers/redfish/provider.py:252` | `asyncio.timeout` around an async generator's yield loop cancels the *consumer's* task, not the `async with`. The handler runs, but `CancelledError` escapes into `IngestService` and past both `except Exception` blocks in `tools/run_collector.py`. Measured. | `for finished in asyncio.as_completed(tasks, timeout=self._run_budget):` — drop the `async with`, keep the existing `except TimeoutError` and `finally` drain. Verified. | The run-budget path exits the pod with an unhandled traceback and status 1 instead of the status 3 that means "partial run" — defeating the reason the in-process budget exists. | **H** |
| 3 | `backend/app/infrastructure/providers/ucs_central/client.py:112` | `wait_for(to_thread(...))` cannot stop a running `ucscsdk` call, and `UcscHandle` takes no `timeout` (signature confirmed). The thread and its executor slot are held until the socket gives up. | Set the socket timeout on the handle's underlying driver, or give this client its own `ThreadPoolExecutor` shut down with `wait=False`. Either way, correct the docstring: the deadline bounds the wait, not the call. | A wedged UCS Central leaks worker threads from the shared ~20-slot default executor, and `asyncio.run` then blocks the pod for `THREAD_JOIN_TIMEOUT` = **300 s** after the run has finished, ending in a `RuntimeWarning`. | **H** |
| 4 | `backend/app/infrastructure/providers/oneview/client.py:129`<br>`..openmanage/client.py:92` | No retry or backoff of any kind. OneView's whole run is 3 bulk calls; OME's is 2. | Extract the policy that already exists twice (`intersight/client.py:_backoff_seconds`, `redfish/client.py:_backoff`) into one helper — GET-only, honours `Retry-After`, jittered, never retries 4xx — and use it in all four. | One transient 503 from a busy appliance aborts that appliance's entire collection with no partial result. Both collectors are unproven against live hardware, so this will first be seen in production. | **M** |
| 5 | `backend/app/infrastructure/providers/intersight/client.py:166` | `verify=False`, unconditional, with `settings.py` explicitly refusing to add any override. | Keep `False` as the default (the user's decision stands) but add `INVENTORY_INTERSIGHT_CA_BUNDLE` so an on-prem appliance with an internal-CA certificate can be verified. Put the choice back to the user before changing anything. | Every Intersight request, including the signed one, goes to whatever answers at the endpoint. An on-prem appliance is precisely the box that *can* hold a real internal-CA certificate. | **M** |
| 6 | `backend/app/config/settings.py:203,250`<br>`..oneview/client.py:132`, `..openmanage/client.py:95` | `verify_tls` defaults to `False` with no CA-bundle setting, no per-run WARNING, and no TLS floor — none of the three things that make the Redfish opt-out defensible. | Add `oneview_ca_bundle` / `ome_ca_bundle`, log the same WARNING the Redfish provider does when verification is off, and apply `redfish_tls_min_version` as a shared floor. | "Turn it on where a trusted chain exists" is unreachable without a bundle setting, so it stays off; and nothing in the logs says it is off. Note `S501` does not flag `verify=verify_tls`. | **M** |
| 7 | `backend/app/infrastructure/providers/oneview/provider.py:365` | `asyncio.gather` over all PSU fetches: nothing is returned until every call finishes, and any exception raised outside `fetch`'s own `try` (lines 358-363) propagates and kills the whole PSU pass. | Give it the shape the other two collectors use — `create_task` + `as_completed`, semaphore inside the task — or at minimum `return_exceptions=True` and move the parsing inside the guarded block. | At the ~2500-call scale the setting's comment describes, a slow appliance costs the whole PSU pass, and one malformed body costs it outright. | **M** |
| 8 | `..openmanage/client.py:151`<br>`..oneview/client.py` (logout) | Session teardown is not shielded from cancellation, unlike `redfish/client.py:249`. | Copy the Redfish pattern: `await asyncio.shield(asyncio.wait_for(self._logout(), timeout=10.0))`. | A cancelled run leaks an appliance session. OneView caps at 960 sessions per source IP with a 24-hour idle life; a CronJob leaking one per run erodes that. | **M** |
| 9 | `..oneview/provider.py:356` | `except Exception: return uri, None` discards the error entirely — an auth failure, a timeout and a 500 are indistinguishable, and only an aggregate count is logged. | Keep the containment, but log the exception type and message at DEBUG per URI and include the distinct reasons in the aggregate WARNING. | "37 of 2500 servers' PSUs unreadable" gives an operator nothing to act on. | **L** |
| 10 | `..redfish/client.py:504` | `async def _guard_size` never awaits; it is a pure `len()` check on already-read bytes. | Make it `def` and drop the `await` at line 488. | None functionally — one wasted coroutine per request, and a reader looking for the I/O that isn't there. | **L** |
| 11 | `tools/run_collector.py:535` | `async def _build_provider` never awaits — every factory in `PROVIDER_FACTORIES` is synchronous. | Make it `def` (both call sites are already inside coroutines). | None functionally. Keep it async only if a future provider needs I/O to construct; nothing does today. | **L** |
| 12 | `..intersight/provider.py:333` | `_collect_table` is `async def` with no `await` **by appearance only** — it contains `[row async for row in client.list_all(...)]`, which is a genuine async comprehension the AST sweep does not see through. | **No change.** Listed so the next sweep does not "fix" it. | — | — |
| 13 | `..intersight/client.py:165` | `httpx.Timeout(read_timeout, connect=connect_timeout)` — `write` and `pool` inherit `read_timeout` (60 s) implicitly. | `httpx.Timeout(connect=connect, read=read, write=connect, pool=connect)`. | A wedged pool acquisition waits 60 s instead of 15. Small, but the value is inherited rather than chosen. | **L** |
| 14 | `..oneview/client.py:129` | Default `Limits(100/20)` while `oneview_psu_concurrency` (default 8) drives the fan-out. | `httpx.Limits(max_connections=psu_concurrency + 2, max_keepalive_connections=psu_concurrency + 2)`. | Harmless at 8. If the setting is raised past 20, keepalive churn silently starts costing a TLS handshake per request. | **L** |
| 15 | `..openmanage/provider.py:162` | `health_check` opens and closes a full OME session, and `IngestService.ingest` calls it before `list_servers` opens a second one. | Reuse one client for both, the way the Cisco collectors avoid the double login (`run_collector.py:880-890` records that reasoning for UCS). | Two logins and two TLS handshakes per run against an appliance that counts sessions. | **L** |
| 16 | `tools/loadtest.py:119` | Default `Limits(100/20)` under a caller-chosen `Semaphore(concurrency)`. | Set `limits` from `--concurrency`, or note the ceiling in `--help`. | Above `--concurrency 100` the tool measures its own connection pool rather than the API. | **L** |

### Confirmed correct — do not "fix" these

Recorded so a later pass does not undo deliberate work:

- `..redfish/client.py:249` — shielded, bounded logout. The model for #8.
- `..redfish/provider.py:340-342` — semaphore acquired **outside** the
  per-host budget. Reversing it makes every queued host time out with no
  packet sent.
- `..redfish/provider.py:280` — `gather(..., return_exceptions=True)` in a
  `finally` to drain cancelled tasks so each runs its teardown.
- `..redfish/provider.py:245` — the shuffle. Without it the run budget
  truncates the same slow hosts every run, leaving them permanently stale.
- `..intersight/provider.py` — polled `_over_budget` instead of
  `asyncio.timeout`. Immune to finding #2 by construction.
- `..intersight/client.py` and `..redfish/client.py` retry policies —
  GET-only, `Retry-After`-aware, jittered, never retrying 4xx or `SSLError`.
- `follow_redirects=False` everywhere, explicit where a session token
  travels on the request.
- `asyncio.to_thread`'s contextvars copy, which is what carries structlog's
  bound context into a worker thread. Any hand-rolled executor must
  reproduce it with `contextvars.copy_context().run`.

---

## Sources

Primary only, as required.

- CPython `asyncio` documentation (3.13): "Coroutines and Tasks" —
  `asyncio.gather` `return_exceptions` semantics and the explicit statement
  that other awaitables are not cancelled; "Task Groups" — sibling
  cancellation and `ExceptionGroup`; "Timeouts" — `asyncio.timeout` cancels
  the *current task*; `asyncio.as_completed(aws, *, timeout=None)`.
- PEP 654, *Exception Groups and except\** — the model `TaskGroup` reports
  failures through.
- PEP 3156, *Asynchronous IO Support Rebooted* — the coroutine/Future model.
- CPython stdlib as installed (3.13.12): `asyncio/threads.py`
  (`to_thread` → `contextvars.copy_context()` → `run_in_executor(None, …)`),
  `asyncio/base_events.py` (`shutdown_default_executor(timeout)`),
  `asyncio/constants.py` (`THREAD_JOIN_TIMEOUT = 300`),
  `concurrent/futures/thread.py` (default `max_workers = min(32, cpu+4)`),
  `concurrent.futures.Future.cancel` (cannot cancel a running call).
- httpx documentation (python-httpx.org), via context7: "Resource Limits"
  (defaults 20 keepalive / 100 total / 5 s expiry), "Timeouts — fine tuning
  the configuration" (connect/read/write/pool), `Client`/`AsyncClient` API
  reference (`verify` accepts `True`/`False`/`ssl.SSLContext`; `http2`
  defaults `False`).
- httpx 0.28.1 as installed: `AsyncClient.__init__` signature,
  `DEFAULT_TIMEOUT_CONFIG`, `DEFAULT_LIMITS`, `DEFAULT_MAX_REDIRECTS`.
- AnyIO 4.11 documentation and source, via context7: `to_thread.run_sync`
  signature and docstring (`abandon_on_cancel`), `docs/threads.rst`
  (default limiter 40; `from_thread.check_cancelled()`),
  `docs/synchronization.rst` (`CapacityLimiter`).
- `requests` documentation: `timeout` accepts a `(connect, read)` tuple
  only; no async API.
- `ucsmsdk` 0.9.18 / `ucscsdk` 0.9.0.8 as installed: `UcsHandle.__init__`
  and `UcscHandle.__init__` signatures.
- This repo: `docs/adr/0007`, `0009`, `0014`, `0016`, `0017`, `0020`,
  `0022`; `docs/cisco-collectors.md`, `docs/hpe-collectors.md`,
  `docs/dell-collectors.md`.
