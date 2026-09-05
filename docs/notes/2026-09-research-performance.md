# Measured performance baseline and how to reproduce it

Research note, 2026-09-06, branch `dev-refactor`. **No source code was
changed to produce this.** Everything below is either a measurement taken
against the running stack or a citation to a primary source.

The governing rule for the hardening pass this feeds is *optimise only
what you measure*. So this note is ordered accordingly: first how to
measure, then what was measured, and only then what the numbers say. A
finding with no measurement attached to it is not in this document.

`docs/adr/0007-scale-verification-and-request-coalescing.md` is the
existing baseline and is treated as authoritative unless a measurement
here contradicts it — one does, and that contradiction is called out
explicitly in [§7.3](#73-adr-0007s-open-search-finding-does-not-reproduce)
rather than quietly folded into a new number.

---

## 1. The measurement rig

Recorded so a future session reproduces the numbers rather than
re-derives the method.

**Hardware/software under measurement.** WSL2 (`6.18.33.2-microsoft-standard-WSL2`),
rootless Podman, MongoDB 8, Redis 8-alpine, CPython 3.13.12,
FastAPI 0.141.1, Pydantic 2.13.4 / pydantic-core 2.46.4, PyMongo 4.17.0,
redis-py 8.0.1, Starlette 1.6.0, uvicorn 0.34.0 with uvloop 0.22.1 and
httptools 0.8.0. Client and server share one laptop, so absolute numbers
are only comparable *to each other*; every conclusion below rests on a
ratio or an A/B, never on an absolute.

```bash
# 1. Stack.  `docker compose` is CLAUDE.md's preferred path but the Docker
#    daemon was not running on this machine, so dev-up.sh (no compose
#    provider needed) was used instead.  Both are fine; they name their
#    containers differently and cannot see each other.
scripts/dev-up.sh up

# 2. Data.  Deterministic, so a rerun compares like with like.
uv run python -m tools.seed_inventory --count 10000 --seed 42   # 60.3s wall
uv run python -m tools.seed_inventory --count 50000 --seed 42   # ~5 min

# 3. API under test.
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8080 \
    --log-level warning

# 4. The repo's own tools.
uv run python -m tools.verify_indexes
uv run python -m tools.loadtest --base-url http://127.0.0.1:8080 \
    --concurrency 20 --requests-per-scenario 200
uv run python -m tools.loadtest --base-url http://127.0.0.1:8080 \
    --concurrency 1  --requests-per-scenario 60     # <- the important one
```

Seeding measured **6.0 ms/server** end to end at 10k (60.3 s wall) and
**4.4 ms/server** at 50k (3 m 42 s wall), consistent with ADR-0007's
~5.5 ms/server. A seeded 50k `servers` collection is **247.7 MB of data
plus 84.3 MB of indexes** (`avgObjSize` 5,660 B) — the whole working set
fits in RAM on any realistic host, which is why no measurement below is
disk-bound and why a production instance with a cold or evicted
WiredTiger cache is explicitly untested (§9).

Raw outputs are under the session scratchpad
`…/scratchpad/perf/`: `loadtest-10k-c1.txt`, `loadtest-10k-c20.txt`,
`loadtest-50k-c1.txt`, `loadtest-50k-c20.txt`, `explain-10k.txt`,
`explain-50k.txt`, `ladder.txt`, `server-side-mean.txt`,
`microbench.txt`, `ab-sync-vs-async-deps.txt`, `ab-metrics-middleware.txt`,
`redis-rtt.txt`, `redis-roundtrips.txt`, `asyncio-debug.log`, and the
py-spy profile `pyspy-list.speedscope.json`.

### 1.1 Run `tools/loadtest.py` at concurrency 1 as well as 20

This is the single most useful methodological change to the existing
setup, and it costs nothing.

`tools/loadtest.py` defaults to `--concurrency 20`. At that setting a
p50 of 50 ms and a p50 of 3 ms look like different services, but they can
be the *same* service — one of them just has 19 requests queued in front
of it on a single event loop. Comparing the two runs separates "each
request is slow" from "requests are queueing", and the discriminator is
whether **req/s changes with concurrency**.

Better still, sweep it. A two-point comparison is noisy — my own 10k and
50k `loadtest` runs disagreed about how much concurrency buys (the 10k
c=20 run showed none, the 50k one showed +31%), which is machine noise on
a shared laptop, not a property of the service. A sweep on one scenario,
back to back, is unambiguous (`concurrency-sweep-50k.txt`, 600 requests
per point, cached default list page, 50k servers):

| concurrency | p50 | p95 | req/s | vs c=1 |
|---|---|---|---|---|
| 1 | 3.72 ms | 6.22 ms | 247.9 | 1.00× |
| 2 | 5.57 ms | 8.09 ms | 329.7 | 1.33× |
| 4 | 11.07 ms | 14.06 ms | 351.7 | **1.42×** |
| 8 | 22.75 ms | 27.32 ms | 339.9 | 1.37× |
| 16 | 44.95 ms | 54.67 ms | 348.0 | 1.40× |
| 32 | 102.85 ms | 126.84 ms | 298.3 | 1.20× |

**Throughput saturates at ~350 req/s from concurrency 4 and then
degrades; latency grows linearly with concurrency throughout.** Past c=4,
every added caller buys queueing delay and nothing else. That is a
saturated single-threaded event loop, and it is what makes §8's first
finding a measurement rather than an assumption. The 1.4× that
concurrency *does* buy is the overlap of the Redis round trip and socket
I/O with CPU work — consistent with the 0.22 ms of Redis in a ~3 ms
request (§5, §7.2).

Run the sweep before and after any change intended to make the service
faster. p50 alone will improve for reasons that have nothing to do with
the change.

---

## 2. Profiling an async service

### 2.1 py-spy — the default choice here

A sampling profiler that "lets you visualize what your Python program is
spending time on without restarting the program or modifying the code in
any way", is "extremely low overhead: it is written in Rust for speed and
doesn't run in the same process as the profiled Python program"
([py-spy README](https://github.com/benfred/py-spy)). `record`,
`top` (live view) and `dump` (current call stack per thread) are the
subcommands that matter; `--subprocesses` follows children, `--native`
covers C extensions.

**It is installable here** — `uv run --with py-spy py-spy --version`
fetched and ran py-spy 0.4.2 in about two seconds. Note the air-gap
consequence: `uv run --with` resolves from an index, so on the air-gapped
host py-spy has to be in the mirror (or the wheel carried in) like
anything else. It does not belong in `[dependency-groups] dev` — it is an
operator tool, not a test dependency.

**The permission trap, hit for real on this machine.** py-spy's own
README: users can "profile without root access by getting py-spy to
create the process… but attaching to an existing process by specifying a
PID will usually require root." Here `/proc/sys/kernel/yama/ptrace_scope`
is `1`, so `py-spy record --pid <uvicorn pid>` failed with
`Permission Denied` and `py-spy dump` with the same. The fix that needs
no `sudo` and no system change is to make the server a *descendant* of
py-spy:

```bash
cd backend && uv run --with py-spy py-spy record \
    --duration 50 --rate 250 --format speedscope \
    --output /tmp/…/pyspy-list.speedscope.json \
    -- ../.venv/bin/python -m uvicorn app.main:app \
       --host 127.0.0.1 --port 8080 --log-level warning
# then drive load against it from a second shell
```

**Read the self-time column, not the cumulative one.** This bit me and is
worth writing down: the cumulative profile below put
`starlette/middleware/base.py:144` at **78.98%**, which reads like the
metrics middleware eating four fifths of the service. It is not — that
frame is on the stack for every downstream call. The A/B in §6.2 measured
its real cost at **+0.07 ms**. Cumulative percentages in an async service
mostly re-describe the middleware stack.

### 2.2 `cProfile` + `pstats`, and why it misleads here

`cProfile` is "a C extension with reasonable overhead that makes it
suitable for profiling long-running programs" and is a *deterministic*
profiler
([CPython docs](https://docs.python.org/3/library/profile.html)). Two
problems for this service, neither of which is a bug in `cProfile`:

1. It attributes **wall time to whatever coroutine happens to be
   resumed**. `await`ing a Mongo query does not stop the clock; the time
   is charged to the frames that run while the loop is servicing other
   tasks. In a service where every request path is `await`-heavy, the
   resulting ranking is not the ranking of CPU cost.
2. Per-call instrumentation overhead is paid on *every* call, and this
   path makes a great many small calls (pydantic-core, dependency
   resolution). That inflates exactly the frames you are trying to
   compare.

It is still the right tool for a synchronous hot function measured in
isolation — which is what §5's micro-benchmarks do, using
`time.perf_counter` because the functions there are big enough not to
need call-level attribution.

### 2.3 scalene

Worth knowing about and not used here. It "separates out time spent in
Python from time in native code (including libraries)", "separates out
system time, making it easy to find I/O bottlenecks", profiles memory
per line, and reports overhead "typically no more than 10-20%"
([scalene README](https://github.com/plasma-umass/scalene)). The
Python-vs-native split is genuinely useful for this codebase, where the
question "is pydantic-core the cost, or the Python around it?" comes up
repeatedly — but scalene launches the program (`scalene run prog.py`)
rather than attaching, so it has the same descendant constraint py-spy
has here, with more overhead. py-spy answered the question first.

### 2.4 `pytest-benchmark` for regression guarding

"This plugin provides a benchmark fixture. This fixture is a callable
object that will benchmark any function passed to it", with "sensible
defaults and automatic calibration for micro-benchmarks" and "comparison
and regression tracking"
([pytest-benchmark docs](https://pytest-benchmark.readthedocs.io/en/latest/)).

Where it would earn its keep in *this* repo is not the API — a network
benchmark in CI is noise — but the two pure functions the numbers below
say are hot and are trivially benchmarkable in-process:
`ServerSummary.from_server` over a fixed 50-document fixture, and
`Server.model_validate` over one stored document. Both are deterministic,
both are on the hot path, and both would regress silently today.

---

## 3. Finding event-loop blocking

### 3.1 What actually blocks

Anything that runs to completion without yielding to the loop, i.e. any
plain function call that is not fast. In this codebase's real and
plausible shapes:

- **Synchronous drivers.** `ucsmsdk` and every other vendor SDK is
  synchronous; CLAUDE.md already mandates `asyncio.to_thread` for these
  (`app.infrastructure.providers.ucs_manager.client`). The collectors run
  in CronJobs, not the API process, so this cannot block the API loop —
  but it can serialise a collector run.
- **`time.sleep`** anywhere in an async path (none found).
- **CPU loops** — regex evaluation over many servers, the in-memory pivot
  in `app.api.v1.sites`, health-policy family resolution.
- **Large `json.dumps`/`json.loads`.** Measured, and real here: 0.27 ms
  to `json.loads` one 55 KiB cached list page (§5). That is not a "block"
  at 100 ms scale, but 200 of them per second is 5% of a core.
- **`bcrypt`-style KDF work.** Not applicable yet — there is no
  authentication (CLAUDE.md convention 6). It becomes the single most
  likely event-loop stall the day auth lands: a correctly-tuned password
  hash is *designed* to take ~100 ms of CPU. Whoever writes that must put
  it in a thread.
- **DNS.** `socket.getaddrinfo` is blocking; both drivers here resolve
  through their own async paths, and both endpoints are configured as
  literals or in-cluster names.

### 3.2 How to detect it rather than eyeball it

`asyncio` debug mode. It is enabled by "setting the `PYTHONASYNCIODEBUG`
environment variable to `1`", by `-X dev`, by `asyncio.run(debug=True)`,
or by `loop.set_debug()`. Then: "Callbacks taking longer than 100
milliseconds are logged. The `loop.slow_callback_duration` attribute can
be used to set the minimum execution duration in seconds that is
considered 'slow'."
([CPython asyncio docs](https://docs.python.org/3/library/asyncio-dev.html))

Run it against this service like so — note `--loop asyncio`, because
uvicorn selects uvloop by default and `PYTHONASYNCIODEBUG` is a CPython
`asyncio` feature:

```bash
cd backend && PYTHONASYNCIODEBUG=1 uv run uvicorn app.main:app \
    --port 8082 --loop asyncio --log-level warning 2> asyncio-debug.log
```

**Measured result: exactly one warning fired, and it was startup, not a
request.**

```
Executing <Task pending name='Task-1' coro=<Server.serve() …>> took 0.611 seconds
```

That is `app.main`'s lifespan — `ensure_indexes` plus
`ensure_default_classification_rules`/`_health_policies`. Under a
deliberately cache-flushed load (uncached `/api/v1/sites`,
`/api/v1/servers/facets`, 200-row pages, then 40 concurrent misses) **no
request-path callback exceeded the 100 ms default.**

**And that is the limitation worth recording.** `slow_callback_duration`
finds *one long block*. It cannot find *many short ones*, which is
precisely what this service has: ~2.7 ms of CPU per request, 200+ times a
second, never once crossing 100 ms. A service can be completely CPU-bound
and produce a perfectly clean asyncio-debug log. The two tools that do
find it are py-spy (§2.1) and the concurrency-1-vs-20 comparison (§1.1).
Lower `slow_callback_duration` to ~0.005 if you want it to speak at all
here, and expect a firehose.

---

## 4. MongoDB

### 4.1 Reading `explain("executionStats")`

Field definitions, from the
[MongoDB manual](https://www.mongodb.com/docs/manual/reference/explain-results/):

- `nReturned` — "Number of documents returned by the winning query plan."
- `totalKeysExamined` — "Number of index entries scanned."
- `totalDocsExamined` — "Number of documents examined during query
  execution. Common query execution stages that examine documents are
  `COLLSCAN` and `FETCH`."
- `executionTimeMillis` — "Total time in milliseconds required for query
  plan selection and query execution."

The number to look at is not any one of these but the **ratio**
`totalDocsExamined / nReturned`. 1:1 is a query doing no wasted work.
`COLLSCAN` means every document was examined; `IXSCAN` means index keys
were scanned; `FETCH` is the document load after an index hit; a `SORT`
stage in the winning plan means **MongoDB could not satisfy the sort from
an index and is sorting in memory** — the stage carries `memLimit`
(104,857,600 bytes here) and `usedDisk`, and a spill to disk is the
failure mode that matters at scale.

A **covered query** is one answered from the index alone —
`totalDocsExamined: 0` with a non-zero `nReturned`, no `FETCH` stage. It
is not achievable for `/api/v1/servers`: the endpoint returns whole
`ServerSummary` projections, not just the indexed keys, so every plan
must `FETCH`. Worth stating so nobody chases it.

**PyMongo gotcha, already documented in `tools/verify_indexes.py:245`
and re-confirmed here:** the async `AsyncCursor.explain()` takes **no**
verbosity argument (`explain("executionStats")` raises `TypeError`) and
returns execution stats anyway.

### 4.2 The ESR guideline, and why this repo's indexes already follow it

MongoDB's guideline: "Ensure that equality fields always come first.
Placing equality fields first keeps the remaining index fields in sorted
order… If avoiding in-memory sorts is critical, place sort fields before
range fields (ESR). If your range predicate in the query is very
selective, then put it before sort fields (ERS)."
([MongoDB ESR guideline](https://www.mongodb.com/docs/manual/tutorial/equality-sort-range-guideline/))

`app.infrastructure.mongodb.indexes` states its own rule as "One compound
index per filter whitelisted in `FILTER_FIELDS`, each ending in the
default sort field + `_id`", which is ESR with the range slot empty:
`(site_id, name_normalized, _id)`, `(identity.vendor, name_normalized,
_id)` and their five siblings are all `(E, S, S)`. The measurements in
§7.2 confirm the planner uses them.

### 4.3 How keyset pagination interacts with index choice

`MongoServerRepository.list_page` builds, for page 2+, an `$or` of
`{sort_field: {$gt: v}}` and `{sort_field: v, _id: {$gt: id}}`, and sorts
`[(sort_field, dir), ("_id", dir)]`. Measured plan at 10k
(`explain-10k.txt`):

```
cursor page 2, sort=name   nRet=51 keys=51 docs=51  0ms
  SUBPLAN <- LIMIT <- FETCH <- SORT_MERGE <- IXSCAN[name_id] <- IXSCAN[name_id]
```

The `$or` is answered by the **subplanner**, which runs each `$or` branch
against `name_id` separately and `SORT_MERGE`s the two already-ordered
streams. That is why the `(sort_field, _id)` shape matters: `SORT_MERGE`
merges *sorted* inputs, so no blocking `SORT` stage appears, and
`keys == docs == nReturned == 51`. This is the good case and it is
holding.

The consequence for anyone changing an index: a `(sort_field, _id)` pair
is not decoration, it is what keeps the cursor query out of a blocking
sort. ADR-0007 already found this the hard way with `last_seen_at`.

### 4.4 Datetimes are ISO 8601 strings

Per ADR-0006, every `datetime` is stored as an ISO 8601 **string**
(`model_dump(mode="json")`). Two consequences that are easy to get wrong
and are worth restating next to the index work:

- A range or cursor query must compare against a **string**, not a parsed
  `datetime`. A `datetime` comparand against a string field matches
  nothing in MongoDB's BSON type ordering — silently, with an empty
  result rather than an error.
- The `last_seen_at_id` and `updated_at_id` indexes are therefore string
  indexes. ISO 8601 with a fixed offset sorts lexicographically the same
  as chronologically, which is what makes this work at all. A document
  written with a different offset (`+03:00` vs `Z`) would sort wrong.
  Everything currently written is UTC `Z` via `app.utils.timeutil.utcnow`.
  This is a latent constraint, not a current bug, and no measurement here
  tests it.

---

## 5. Redis round-trip batching

Redis is a request/response protocol; pipelining "is possible to send
multiple commands to the server without waiting for the replies at all,
and finally read the replies in a single step", and the docs report a
loopback benchmark improving "by a factor of five", with throughput that
"initially increases almost linearly with longer pipelines, and
eventually reaches 10 times the baseline"
([Redis pipelining](https://redis.io/docs/latest/develop/using-commands/pipelining/)).
The docs are equally clear about when pipelining *cannot* help:
"pipelining can't help in this scenario since the client needs the reply
of the read command before it can call the write command."

**Measured round-trip cost here** (`redis-rtt.txt`, redis-py 8.0.1 to a
local container, 1,000 iterations):

| operation | mean | p50 |
|---|---|---|
| `PING` | 0.211 ms | 0.212 ms |
| `GET` of a 55 KiB value | 0.223 ms | 0.220 ms |

So a round trip costs ~0.21 ms and the payload is nearly free at this
size — the cost is the round trip, exactly as the Redis docs describe.

**Measured round trips per request** (`redis-roundtrips.txt`, via
`redis-cli info commandstats` deltas over 100 requests each):

| endpoint | Redis calls / request |
|---|---|
| `GET /api/v1/servers` | 1.00 `get` |
| `GET /api/v1/servers/facets` | 1.00 `get` |
| `GET /api/v1/sites` | 1.00 `get` |
| `GET /api/v1/servers/{id}` | **2.00 `get`** |

**There is no N+1 in the cache-aside read path.** The place an N+1 would
normally hide — a per-row cache lookup while building a list page —
does not exist here, because the list page is cached whole rather than
per row. That is a design decision worth not undoing.

The detail endpoint's 2 round trips are the `_revision_pointer_key` →
`server_key(id, revision)` chain the module docstring describes. They are
inherently **sequential** (the second key is computed from the first
reply), so this is exactly the case the Redis docs say pipelining cannot
address. It costs ~0.21 ms and is not on the list-page hot path; noted,
not proposed as a fix.

---

## 6. FastAPI response serialisation

### 6.1 What FastAPI 0.141.1 actually does — read the installed source

`fastapi/routing.py`'s `serialize_response` runs
`field.validate(response_content, {}, loc=("response",))` before
serialising. So **yes, `response_model` re-validates on the way out.**
But the installed source also shows a fast path that changes the advice
most sessions would give:

```python
# fastapi/routing.py, ~line 719
# Use the fast path (dump_json) when no custom response class was set and
# a response field with a TypeAdapter exists. Serializes directly to JSON
# bytes via Pydantic's Rust core, skipping the intermediate Python dict +
# json.dumps() step.
use_dump_json = response_field is not None and isinstance(
    response_class, DefaultPlaceholder
)
```

Two things follow, both of which contradict the usual folk advice:

1. **Do not add `ORJSONResponse` to this app.** Setting a response class
   makes `response_class` no longer a `DefaultPlaceholder`, which
   **disables** the `dump_json` fast path — you would trade
   pydantic-core's Rust serialiser for a Python dict plus `orjson`.
   FastAPI's own reference now says the same thing: ORJSONResponse "is
   deprecated because FastAPI serializes data directly to JSON bytes via
   Pydantic when a return type or response model is defined, which
   provides faster serialization without requiring a custom response
   class"
   ([FastAPI responses reference](https://fastapi.tiangolo.com/reference/responses)).
   `orjson` is not currently a dependency; it should stay that way.
2. **The re-validation is nearly free when you hand FastAPI the model
   instance it already expects.** Measured: `TypeAdapter(ServerListResponse)
   .validate_python(<a ServerListResponse instance>)` costs **0.000 ms** —
   pydantic short-circuits on an exact model instance. The commonly-cited
   "Pydantic v2 re-validates your response, so it costs you twice" is not
   true on this code path. Quantified so nobody optimises it.

### 6.2 The middleware A/B, which disproved my own hypothesis

`app.main` registers the metrics middleware with `@app.middleware("http")`,
i.e. Starlette's `BaseHTTPMiddleware` — while `RequestContextMiddleware`
deliberately does not, its docstring saying "Pure-ASGI middleware (not
BaseHTTPMiddleware) to avoid its known interaction problems". The py-spy
cumulative profile put `middleware/base.py` at 78.98%, which looks
damning.

A/B with `INVENTORY_METRICS_ENABLED=false` on a second port
(`ab-metrics-middleware.txt`, 400 requests each):

| endpoint | metrics ON | OFF | delta |
|---|---|---|---|
| `/health/live` | 1.28 ms | 1.22 ms | **+0.07 ms** |
| `/api/v1/servers` (50 rows) | 3.56 ms | 3.43 ms | **+0.13 ms** |

**Not a bottleneck.** Recorded because the profile said otherwise and the
A/B is what settled it — and because a future session reading that
profile will reach for the same wrong conclusion.

---

## 7. The measured baseline

All numbers below are with the API on `127.0.0.1:8080`, one uvicorn
worker, Mongo and Redis in local containers.

### 7.1 The headline: the read path is CPU-bound in one event loop

`tools/loadtest.py` against **50,000** servers (`loadtest-50k-c1.txt`,
`loadtest-50k-c20.txt`):

| scenario | c=1 p50 | c=1 req/s | c=20 p50 | c=20 p99 | c=20 req/s |
|---|---|---|---|---|---|
| no filter, default sort | 3.3 ms | 285 | 50.1 ms | 86.5 ms | 373 |
| filter=vendor sort=name | 3.2 ms | 295 | 51.4 ms | 97.7 ms | 375 |
| filter=installation_type | 3.2 ms | 291 | 52.0 ms | 88.4 ms | 375 |
| filter=health_overall | 3.3 ms | 284 | 50.9 ms | 107.0 ms | 377 |
| search (selective)¹ | 2.2 ms | 434 | 33.7 ms | 165.8 ms | 455 |
| search (low-selectivity) | 3.3 ms | 271 | 50.0 ms | 90.6 ms | 383 |
| sort=last_seen_at | 3.2 ms | 295 | 50.4 ms | 91.3 ms | 383 |
| page_size=100 | 4.6 ms | 186 | 75.0 ms | 139.6 ms | 239 |

The same run at **10,000** servers (`loadtest-10k-*.txt`) is within noise
of it — 4.1 ms / 235 req/s at c=1, 89.1 ms / 214 req/s at c=20 for the
default page. **Latency and throughput are essentially independent of
collection size across the 10k → 50k range**, which is what you expect
when every query is an index-bounded 51-key scan (§7.2) and the cost is
in Python.

Twenty times the concurrency; twenty times the p50; throughput up 1.3×,
not 20×. The service is not waiting on Mongo or Redis, it is saturating
one CPU — see the sweep in §1.1, which pins the ceiling at ~350 req/s.
Every entry above is a *cache hit* (200 identical requests inside a 15 s
`LIST_PAGE_TTL_SECONDS` window), so this is the cheap path, not the
expensive one.

Also note the p95/p99 story ADR-0007 documents is **gone**: nothing here
is in the seconds, at either scale. Request coalescing plus the cache are
doing their job; what remains is per-request CPU.

¹ `tools/loadtest.py`'s "search (selective)" scenario searches for
`ocp-dell-worker-0001`, which **does not exist** in a `--seed 42` estate
(`nReturned: 0`, confirmed by explain). That row measures the
empty-result case, not a one-hit case. Worth fixing in the tool before
the next baseline; left alone here so these numbers stay comparable to
ADR-0007's.

### 7.2 Where the CPU goes

Server-side duration taken from the app's **own** Prometheus histogram
(`http_request_duration_seconds`), which excludes client and socket cost
(`server-side-mean.txt` at 10k, `server-side-mean-50k.txt` at 50k;
400 requests each, all cache hits):

| request | @10k | @50k |
|---|---|---|
| `/health/live` (framework floor, no deps, no I/O) | **0.188 ms** | 0.336 ms |
| `/api/v1/servers?page_size=1` | **1.588 ms** | 2.138 ms |
| `/api/v1/servers` (50 rows) | **2.702 ms** | 3.326 ms |
| `/api/v1/servers?page_size=200` | **6.709 ms** | 6.583 ms |

Marginal cost per row at 10k: `(6.709 − 1.588) / 199` = **0.0257 ms/row**.
Fixed cost per request: **~1.6–2.1 ms**, of which only 0.19–0.34 ms is
FastAPI/Starlette itself. So at the default page size, **59–64% of the
request is fixed overhead** that has nothing to do with how many servers
were returned.

And the **cache-miss** cost, measured by flushing Redis before every
request (`withcount-50k.txt`, 25 requests each, 50k servers):

| request | server-side mean |
|---|---|
| `/api/v1/servers` (cache hit, 50 rows) | 3.33 ms |
| `/api/v1/servers` (**cache miss**, 50 rows) | **12.14 ms** |
| `/api/v1/servers?with_count=true` (**cache miss**) | **32.25 ms** |

A miss costs ~3.6× a hit. `with_count=true` costs **another ~20 ms**,
because `MongoServerRepository.list_page` answers it with
`count_documents(base_filter)` — an unfiltered count is a `COLLSCAN` over
all 50,000 documents, which `tools/verify_indexes.py` reports (correctly)
as an expected one:

```
OK   servers: count_documents({}) [with_count=True path]
      stages=['COLLSCAN'] index=(none) in_memory_sort=False
      keysExamined=0 docsExamined=50000 returned=50000
```

That is the single largest Mongo cost in the whole read path and the only
number here that grows linearly with the estate.

py-spy self-time over a 39 s profile of the same path
(`pyspy-list.speedscope.json`; 9,758 samples on the event-loop thread):

| self time | frame |
|---|---|
| 14.32% | `model_validate` (`pydantic/main.py:732`) |
| 11.61% | `dump_json` (`pydantic/type_adapter.py:677`) |
| 10.38% | `raw_decode` (`json/decoder.py:361`) |
| 9.75% | `run` (`asyncio/runners.py:118`) — loop idle/select |
| 2.89% | `app` (`fastapi/routing.py:144`) |
| 2.32% | `emit` (`logging/__init__.py:1154`) |
| 1.99% | `notify` (`threading.py:414`) |
| 1.56% | `uuid4` (`uuid.py:716`) |

and the cumulative frames that identify the call sites:

| cumulative | frame |
|---|---|
| 15.81% | `list_servers` **`v1/servers.py:213`** — `await cache.get(cache_key)` |
| 14.40% | `list_servers` **`v1/servers.py:215`** — `ServerListResponse.model_validate(cached)` |
| 12.84% | `serialize_response` (`fastapi/routing.py:330`) |
| 11.01% | `CacheClient.get` (`redis/cache.py:78`) → 10.56% `json.loads` |
| 7.63% | `run_in_threadpool` (`starlette/concurrency.py:34`) |

Isolated micro-benchmark of each stage, on a real 50-document page
(`microbench.txt`, 200 reps each):

| stage | mean |
|---|---|
| `Server.model_validate(doc)` × 50 (repository, **cache miss only**) | 2.715 ms |
| `ServerSummary.from_server` × 50 | 0.139 ms |
| `response.model_dump(mode="json")` | 0.347 ms |
| `json.dumps` for `cache.set` | 0.232 ms |
| — cached payload size | 54.9 KiB |
| `json.loads` on cache hit | 0.271 ms |
| `ServerListResponse.model_validate(cached)` | 0.313 ms |
| FastAPI `field.validate(response)` | **0.000 ms** |
| FastAPI `serialize_json(value)` | 0.326 ms |
| **whole cache-hit chain** (loads → validate → validate → dump) | **0.919 ms** |

So of the 2.70 ms server-side cache hit: ~0.92 ms is the JSON/Pydantic
round trip, ~0.42 ms is the sync-dependency threadpool (§7.4), ~0.22 ms
is the Redis round trip, ~0.19 ms is the framework floor, and the
remainder is routing, `stable_hash`, request-id/uuid4 and the structured
access log.

The **cache-miss** path adds `Server.model_validate` at **2.7 ms per
50-document page** — measurably the most expensive single thing in the
codebase's read path, and it exists only so the repository can return
domain objects that the handler immediately projects down to
`ServerSummary`.

### 7.3 ADR-0007's open search finding: mechanism confirmed, pathology not reproduced

This is the one place a measurement disagrees with the existing baseline,
so it gets stated carefully rather than as a headline.

ADR-0007's "related, deliberately undecided finding" says three things:
(a) a zero/near-zero-match search "must examine the *entire* collection";
(b) the mechanism is that "the sort field's index drives the scan; the
search filter is applied as an in-memory `FETCH`-stage regex check"; and
(c) p99 ≈ 700–800 ms at 50k. It further predicts that forcing the planner
onto `search_tokens` "would trade this problem for a blocking in-memory
sort".

**The planner picks between exactly those two plans, per query, and the
choice flips with scale.** Measured (`explain-10k.txt`,
`explain-50k.txt`), same three search shapes at both sizes:

| shape | 10k | 50k |
|---|---|---|
| `^ocp-dell` (low selectivity, 1,570 matches at 50k) | `SORT <- FETCH <- IXSCAN[search_tokens]`, keys 314 / docs 313 / **3 ms** | `LIMIT <- FETCH <- IXSCAN[name_id]`, keys 1,604 / docs 1,604 / **24 ms** |
| `^ocp-dell-worker-0001` (no match in this seed) | `SORT <- FETCH <- IXSCAN[search_tokens]`, keys 1 / docs 0 / 0 ms | same, keys 1 / docs 0 / **0 ms** |
| `^zzzznope` (no match) | `SORT <- FETCH <- IXSCAN[search_tokens]`, keys 0 / docs 0 / 0 ms | same, keys 0 / docs 0 / **1 ms** |

So:

- **(b) is confirmed, at 50k, for the low-selectivity case.** The 50k plan
  is `IXSCAN[name_id]` with the regex applied as a `FETCH`-stage filter,
  examining **1,604 documents to return 51** — precisely the mechanism
  ADR-0007 describes. At 10k the planner instead chose `search_tokens`
  plus a blocking `SORT` and examined 313. ADR-0007's prediction about
  what a `search_tokens` hint would do is therefore not hypothetical:
  MongoDB does it on its own below some cost threshold.
- **(a) and (c) did not reproduce.** The zero-match searches are the
  *cheapest* queries in the set at both scales — 0 keys, 0 documents,
  ≤1 ms — because the planner picks `search_tokens` for them, where a
  non-matching anchored prefix terminates immediately. Nothing here goes
  near 700–800 ms; the c=1 p99 for a search at 50k is 24.3 ms, and that
  is the *low*-selectivity one.

Three caveats, all of which matter before anyone edits ADR-0007:

1. `explain()` reports the plan chosen **now**. MongoDB's planner is
   cost-based with a plan cache, so a different data distribution, a cold
   cache, or a different query order can pick differently. The flip
   between 10k and 50k above is itself the demonstration.
2. The seed is not the same estate ADR-0007 measured. `--seed 42` at 50k
   puts 1,570 servers behind `^ocp-dell` — 3% of the fleet, not the
   "roughly a quarter" ADR-0007 describes. A term matching 12,500
   documents is a materially different query and was **not** measured
   here.
3. ADR-0007 measured before request coalescing existed for the miss path
   and its numbers are p99 under 20 concurrent callers, whereas the
   explains above are single queries. They are not the same measurement.

**The action is an update to ADR-0007 recording the current plans and the
seed caveat — not a code change, and not a claim the original measurement
was wrong.** Its central decision (coalescing) is unaffected either way.

### 7.4 Sync dependency functions cost a threadpool hop each

Every dependency in `app/dependencies.py` — `get_mongo_holder`,
`get_redis_holder`, `get_request_id`, `get_current_actor` — and every
provider in `app/api/v1/servers.py` — `_server_repo`, `_cache_client`,
`_regex_engine`, `_classification_service`, … — is declared `def`, not
`async def`. FastAPI runs a sync dependency in the anyio worker
threadpool. `list_servers` declares three of them.

A/B in an isolated FastAPI app, 2,000 requests each over `ASGITransport`
(`ab-sync-vs-async-deps.txt`):

| endpoint | mean | p50 |
|---|---|---|
| no dependencies | 0.276 ms | 0.268 ms |
| 3 × `async def` dependencies | 0.340 ms | 0.313 ms |
| 3 × `def` dependencies | **0.763 ms** | 0.719 ms |

**+0.42 ms per request, ~0.14 ms per sync dependency**, purely in
threadpool handoff. Against the 2.70 ms measured server-side cost of a
cached list page that is **~16%**. It matches the profile independently:
`run_in_threadpool` at 7.63% cumulative plus `threading.notify` at 1.99%
self.

---

## 8. Findings, each tied to a measurement

Ordered by measured size. None of these is a proposal to implement — the
brief for this note was to establish a baseline, and a fix belongs in its
own change with its own before/after.

1. **The service saturates one event loop at ~350 req/s, and collection
   size barely matters.** Evidence: §1.1's sweep (flat from c=4 to c=32,
   then degrading) and §7.1 (10k and 50k within noise of each other).
   This is the finding that frames every other one: with one uvicorn
   worker the ceiling is ~350 list requests/s whatever Mongo does. It is
   also the cheapest thing to fix, and the fix is not in this codebase —
   it is `--workers N` / more replicas, which the deployment does not do
   yet (CLAUDE.md's not-done item 2, no frontend or multi-worker
   manifests). **Measure the multi-worker number before optimising a
   single line of Python**; the per-request costs below are worth roughly
   10–20% each, and a second worker is worth 100%.

2. **~60% of a cached list request is fixed per-request overhead, not
   payload.** Evidence: §7.2 — 1.588 ms at `page_size=1` versus 2.702 ms
   at 50 rows (10k); 2.138 vs 3.326 ms (50k). Anything that reduces
   per-request fixed cost is worth more than anything that reduces
   per-row cost, at the page sizes actually used.

3. **Sync dependency functions cost +0.42 ms/request (~16%).**
   Evidence: §7.4, an isolated A/B plus two independent profile frames.
   This is the largest single measured cost with a one-keyword remedy.

4. **The cache-hit path decodes and re-validates JSON it is about to
   re-encode unchanged: 0.919 ms/request.** Evidence: §7.2's
   micro-benchmark, corroborated by py-spy self-time
   (`model_validate` 14.32% + `dump_json` 11.61% + `raw_decode` 10.38%).
   `CacheClient.get` does `json.loads`, the handler does
   `ServerListResponse.model_validate`, and FastAPI then `dump_json`s it
   straight back out — the bytes in Redis are already the bytes on the
   wire. Note the two *non*-costs measured alongside it: FastAPI's
   response re-validation is 0.000 ms, and `ORJSONResponse` would make
   this worse, not better (§6.1).

5. **`Server.model_validate` is 2.7 ms per 50-row page on a cache miss**,
   and a miss is 12.14 ms against 3.33 ms for a hit at 50k. Evidence:
   §7.2. The repository validates a full domain `Server` per document so
   the handler can project it to a much smaller `ServerSummary` and throw
   the rest away.

6. **`with_count=true` costs ~20 ms at 50k and scales linearly with the
   estate.** Evidence: §7.2 — 32.25 ms versus 12.14 ms on a cache miss,
   and `tools/verify_indexes.py` confirming `count_documents({})` is a
   50,000-document `COLLSCAN`. It is the only measured cost here that
   grows with fleet size, and at 500k it would be the whole request. The
   parameter defaults to `false`, so this is a latent cost rather than a
   current one — but the UI is one checkbox away from paying it on every
   page.

7. **Every declared index is used, and every non-search `/servers` query
   shape at 10k and 50k is `keys == docs == nReturned == 51`.** Evidence:
   §7.2's explain table and a clean `tools/verify_indexes.py` run at 50k.
   There is no index work to do. Say this out loud, because "add an
   index" is where a hardening pass usually starts.

8. **ADR-0007's search finding is half-confirmed and needs an update, not
   a fix.** Evidence: §7.3 — the mechanism it names is exactly the 50k
   plan (1,604 documents examined for 51 returned), but the zero-match
   pathology it quantified is now the cheapest query in the set, and the
   plan flips between 10k and 50k. The action is an ADR update recording
   both plans and the seed caveat.

9. **No N+1 in the Redis read path; no event-loop block above 100 ms.**
   Evidence: §5 and §3.2. Both are negative results and both are worth
   recording so the hardening pass does not spend time there.

---

## 9. What was NOT measured

Stated plainly, because an unmeasured area silently reads as "fine".

- **Any write path.** Ingest, `IngestService`, upsert, audit-event
  append, `POST /reclassify`, `PUT /maintenance`. The 6.0 ms/server
  seeding rate is the only write number here and it is a *batch* rate,
  not a concurrent-write latency.
- **Any collector.** All five run in CronJobs against vendor APIs that
  are not reachable from here. The `asyncio.to_thread` discipline for
  synchronous SDKs is asserted from the code, not measured.
- **Multi-worker / multi-replica throughput.** Finding 1 says this is the
  first thing to measure and it was not measured — everything here is one
  uvicorn worker.
- **Cold-cache and cold-Mongo behaviour.** Every headline number is a
  cache hit against a warm WiredTiger cache with the whole 332 MB working
  set (247.7 MB data + 84.3 MB indexes) resident. A production restart,
  an eviction, or a working set larger than RAM is untested. The
  cache-miss numbers in §7.2 flush *Redis*, not Mongo.
- **A search term matching a large fraction of the fleet.** ADR-0007's
  scenario is a term matching "roughly a quarter" of 50k; `--seed 42`
  puts only 1,570 servers (3%) behind `ocp-dell`. The expensive middle of
  the selectivity curve — thousands of matches, where §7.3's plan flip
  has the most consequence — was not measured.
- **Concurrent read-while-writing.** Every measurement had a quiet
  database. Real deployments have five CronJobs upserting into the
  collection being read.
- **`/api/v1/events` (audit feed) and the classification/health-policy
  routes.** Only `/servers`, `/servers/{id}`, `/servers/facets`,
  `/sites` and `/health/*` were exercised.
- **Memory.** No `scalene`, no `tracemalloc`, no RSS tracking. The
  Intersight collector's in-memory join tables (ADR-0017) are the obvious
  place this would matter and they were not run.
- **The frontend.** No Lighthouse, no bundle analysis, no render
  profiling.
- **100k+.** Headroom past 50k is untested; ADR-0007's target is 50k and
  that is where this stops too.
- **Anything on real deployment hardware.** This is one WSL2 laptop with
  client, server, Mongo and Redis competing for the same cores. Treat
  every absolute number as a ratio.

---

## 10. Stack teardown

The dev stack started for this note (`scripts/dev-up.sh up`, pod
`server-inventory-dev`) and all three uvicorn processes started against
it (ports 8080, 8081, 8082) were **torn down**. Verified after
`scripts/dev-up.sh down`: `podman ps -a` empty, no listener on 8080/8081/
8082/27017/6379, no `uvicorn` process, and `git status --porcelain`
showing no modified tracked file — this note is the only thing added.

The seeded 50,000-server database lived in the pod's volume and went with
it; re-seed with
`uv run python -m tools.seed_inventory --count 50000 --seed 42`.

---

## 11. Sources

All primary.

- py-spy — <https://github.com/benfred/py-spy>
- scalene — <https://github.com/plasma-umass/scalene>
- pytest-benchmark — <https://pytest-benchmark.readthedocs.io/en/latest/>
- CPython, "Developing with asyncio" (debug mode, `slow_callback_duration`)
  — <https://docs.python.org/3/library/asyncio-dev.html>
- CPython, `profile`/`cProfile`/`pstats` —
  <https://docs.python.org/3/library/profile.html>
- MongoDB manual, "Explain Results" —
  <https://www.mongodb.com/docs/manual/reference/explain-results/>
- MongoDB manual, "The ESR (Equality, Sort, Range) Guideline" —
  <https://www.mongodb.com/docs/manual/tutorial/equality-sort-range-guideline/>
- Redis docs, "Redis pipelining" —
  <https://redis.io/docs/latest/develop/using-commands/pipelining/>
- FastAPI reference, "Responses" (ORJSONResponse deprecation) —
  <https://fastapi.tiangolo.com/reference/responses>
- FastAPI, "Custom Response" and "Return a Response Directly" —
  <https://fastapi.tiangolo.com/advanced/custom-response>,
  <https://fastapi.tiangolo.com/advanced/response-directly>
- Installed source, read directly rather than cited from memory:
  `fastapi/routing.py` (`serialize_response`, the `use_dump_json` fast
  path) at FastAPI 0.141.1.
- In-repo: `docs/adr/0006`, `docs/adr/0007`, `tools/loadtest.py`,
  `tools/verify_indexes.py`,
  `app.infrastructure.mongodb.indexes`,
  `app.infrastructure.singleflight`.
