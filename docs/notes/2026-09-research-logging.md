# structlog in an async service — principles, and this repo against them

Research note, 2026-09-06. Sources are primary only: the structlog
documentation, the installed `structlog` 26.1.0 source in
`.venv/lib/python3.13/site-packages/structlog/`, the CPython `logging`
and `asyncio`/`contextvars` docs, and `prometheus_client`. No source
code was changed by this note.

Every claim below is either cited or shown from the installed source.
Where the two disagreed, the installed source wins.

---

## 1. Bound loggers and `structlog.contextvars`

### The three ways to carry context

structlog has three mechanisms and they are not interchangeable:

| Mechanism | Scope | Survives `await`? | Survives a new task? |
|---|---|---|---|
| `logger.bind(k=v)` — a bound logger | the variable you assigned it to | yes (it is just an object) | only if you pass the object |
| `structlog.contextvars.bind_contextvars(k=v)` | the current `contextvars.Context` | yes | into tasks created *after* the bind; never back out |
| a processor that stamps a constant | every line in the process | n/a | n/a |

The repo uses the first (implicitly, via module-level
`structlog.get_logger(__name__)`), the second in exactly one place, and
the third for `service`/`environment`
(`backend/app/infrastructure/logging/config.py:80`).

### What the contextvars module actually offers

From `docs/contextvars.md` and `docs/api.rst`
(https://github.com/hynek/structlog/blob/main/docs/contextvars.md):

```
bind_contextvars(**context)      -> dict of reset Tokens
unbind_contextvars(*keys)
clear_contextvars()
bound_contextvars(**kv)          -> context manager AND decorator
get_contextvars()                -> plain dict of what is bound
merge_contextvars(logger, method_name, event_dict)   # the processor
reset_contextvars(**tokens)
```

The documented general flow is: put `merge_contextvars` first in the
processor chain, `clear_contextvars()` at the start of a request, then
`bind_contextvars` / `bound_contextvars` for the rest.

The docs describe the store as "a global logging context local to the
current execution thread or asynchronous task" — i.e. it is
`contextvars.ContextVar` and inherits Python's own propagation rules,
nothing more.

### Propagation, precisely

These are CPython semantics, not structlog's, and they are what decide
whether a collection run's context reaches a log line:

- **`await`** — an `await` does not change the running Context. A bind
  before an `await` is visible after it, in the same coroutine.
  (`contextvars` — the Context is per-Task, and a coroutine runs inside
  its Task's Context.)
- **`asyncio.create_task(coro)`** — the task runs in a *copy* of the
  Context taken at `create_task()` time
  (https://docs.python.org/3/library/asyncio-task.html#asyncio.Task —
  "The task executes the coroutine in a copy of the current
  contextvars.Context"). So: **binds made before task creation are
  inherited; binds made inside the task are invisible to the parent and
  to sibling tasks.** That isolation is the useful property — parallel
  per-domain or per-host context cannot leak between concurrent
  collections.
- **`asyncio.gather(*coros)`** — each coroutine is wrapped in a Task, so
  it is the `create_task` rule per element.
- **`asyncio.to_thread(fn, ...)`** — the context *is* propagated:
  "the current `contextvars.Context` is propagated, allowing context
  variables from the event loop thread to be accessed in the separate
  thread"
  (https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread).
  This matters here because every `ucsmsdk` call goes through
  `asyncio.to_thread` (`ucs_manager/client.py:104,122,143`;
  `ucs_central/client.py:113`) and therefore keeps the run's context for
  free.
- **A raw `ThreadPoolExecutor` / `concurrent.futures` pool** — does
  **not** propagate. This is why structlog's own recipe
  (https://github.com/hynek/structlog/blob/main/docs/recipes.md) captures
  `structlog.contextvars.get_contextvars()` and re-binds it inside the
  worker via `functools.partial`. That recipe is *not* needed for
  `asyncio.to_thread`, and applying it there would be cargo cult.

### Recommendation for a collection run

Bind run-scoped context once, at the top of `tools/run_collector.py::_run`,
immediately after `configure_logging(...)` (`tools/run_collector.py:907`):

```python
structlog.contextvars.clear_contextvars()
structlog.contextvars.bind_contextvars(
    run_id=f"run_{uuid.uuid4().hex[:12]}",
    manager_type=manager_type.value,
)
# ... after `connection` is resolved:
structlog.contextvars.bind_contextvars(endpoint=connection.endpoint)
```

Everything downstream — the provider, `IngestService`, and every task
`create_task`/`gather` spawns after this point — inherits it, including
the blocking SDK calls behind `asyncio.to_thread`. `clear_contextvars()`
first because the process is long-lived enough in tests to have
leftovers, and because the docs prescribe it as the first step of the
general flow.

Per-item scope goes *inside* the task coroutine, with the context
manager, not with a bare bind:

```python
async def _collect_domain_result(self, target, sem):
    with structlog.contextvars.bound_contextvars(
        domain_id=target.domain_id, domain=target.name
    ):
        ...
```

`bound_contextvars` is correct here for two reasons: it unbinds on exit
even on an exception path, and because each task holds its own Context
copy the bind cannot reach a sibling domain being collected
concurrently. **Never call `clear_contextvars()` inside a task** — it
clears that task's copy only, which reads as a no-op from the parent
and as a silent context loss inside.

The API side already does this correctly:
`backend/app/middleware/request_context.py:58` wraps the whole downstream
call in `bound_contextvars(request_id=request_id)`, and
`merge_contextvars` is first in the chain
(`logging/config.py:53`). That is the textbook shape.

---

## 2. Log levels that mean something

CPython's logging HOWTO gives the canonical definitions
(https://docs.python.org/3/howto/logging.html#when-to-use-logging):
DEBUG "detailed information, typically of interest only when diagnosing
problems"; INFO "confirmation that things are working as expected";
WARNING "something unexpected happened … the software is still working
as expected"; ERROR "due to a more serious problem, the software has not
been able to perform some function"; CRITICAL "a serious error,
indicating that the program itself may be unable to continue running".

Those are correct but under-determined for a service. The decision rule
below is what makes them mechanical.

### The rule

**Level is decided by who must act and when — never by how bad the
sentence sounds.** Two questions, in order:

1. **Can you name the person who acts on this line, and when?**
   - "an on-call human, now, because the job did not do its job" → **ERROR**
   - "whoever reads the weekly trend, because this shouldn't be common" → **WARNING**
   - "nobody; it is the record of a normal run" → **INFO**
   - "me, next time I am debugging this specific thing" → **DEBUG**
2. **How often does it fire?**
   - more than once per run (or per request) → it is not INFO. Aggregate
     it into one summary line, or drop it to DEBUG.
   - once per item, where items scale with fleet size → **DEBUG**, always.

Applied to the two shapes in this repo:

**Batch collector** (one CronJob run, exits with a code)

| Level | What belongs |
|---|---|
| ERROR | *the run did not see the whole fleet* — run budget expired, a paging truncation, a credential circuit opening, the whole manager unreachable. Exactly the conditions that produce exit code 3 or 1. |
| WARNING | the run degraded itself and continued: one host of many unreachable, one subresource unreadable, TLS verification off, a value carried forward instead of read. |
| INFO | run start, **one summary line per run**, the collection plan, and one line per genuinely-once decision (chosen API version, name-filter result). |
| DEBUG | anything per-server or per-HTTP-request. |
| CRITICAL | nothing. A batch job that cannot continue exits with a code; CRITICAL adds a level of alert severity that no consumer here distinguishes from ERROR. The repo has zero CRITICAL calls and that is right. |

**Request-serving API**

| Level | What belongs |
|---|---|
| ERROR | 5xx — *the server* failed. Anything a caller can trigger at will is not ERROR. |
| WARNING | degraded-but-served: Redis down and the read fell through to Mongo, a stale cache entry, a retry that succeeded. |
| INFO | lifecycle (start/ready/stop) and exactly one line per request. |
| DEBUG | per-query, per-document, per-serialization. |
| CRITICAL | reserved for "the process is going down and in-flight work may be lost". |

The sharpest consequence: **a 4xx is never ERROR.** A caller sending
malformed JSON is not a service fault, and an endpoint that logs it at
ERROR trains the on-call to ignore ERROR. `exception_handlers.py`
already gets this right (`:60`, `:80`, `:105` are all INFO; only the
unhandled-exception handler at `:121` is ERROR), as does
`request_context.py:60` (`logger.info if status_code < 500 else
logger.error`).

---

## 3. Exception logging

### `logger.exception` vs `logger.error(..., exc_info=…)`

With `wrapper_class=structlog.stdlib.BoundLogger` (what this repo
configures, `logging/config.py:68`), the installed source settles it —
`stdlib.py`, `BoundLogger.exception`:

```python
def exception(self, event=None, *args, **kw):
    """
    Process event and call `logging.Logger.exception` with the result,
    after setting ``exc_info`` to `True` if it's not already set.
    """
    kw.setdefault("exc_info", True)
    return self._proxy_to_logger("exception", event, *args, **kw)
```

So `logger.exception("x")` is exactly `logger.error("x", exc_info=True)`
plus a `setdefault`, and both reach the same processors. There is no
behavioural reason to prefer one — pick one for consistency. Note
`setdefault`, not overwrite: `logger.exception("x", exc_info=some_exc)`
does honour the explicit exception, which is what you want when handling
an exception captured elsewhere.

`logger.error(..., exc_info=exc)` (an exception *instance*, not `True`)
is legitimate and is what `exception_handlers.py:125` does — correct
there, because the handler receives `exc` as a parameter rather than
being inside its `except` block, so `sys.exc_info()` is not guaranteed
to be the right one.

Because the repo also uses `_drop_sensitive_keys`, note that
`exc_info` is a *key in the event dict* until a renderer consumes it —
it passes through every processor before `format_exc_info`.

### `format_exc_info` vs `dict_tracebacks` vs `ExceptionRenderer`

From `docs/exceptions.md`
(https://github.com/hynek/structlog/blob/main/docs/exceptions.md):
`structlog.processors.ExceptionRenderer` is the general machinery — it
deduces `exc_info`, removes it from the event dict, passes it to a
formatting function, and stores the result under the `exception` key.
`format_exc_info` and `dict_tracebacks` are the two pre-built
instantiations of it.

What each does to a **JSON log consumer**:

- **`format_exc_info`** (what this repo uses, `logging/config.py:59`) —
  `exception` becomes one multi-line **string**:
  `"Traceback (most recent call last):\n  File …"`. In JSON this is a
  single field with embedded `\n`. Every consumer can store it; none can
  query it. You cannot filter by exception type, and a log UI shows it
  as one long escaped blob unless it special-cases the field.
- **`dict_tracebacks`** — `exception` becomes a JSON **array of frame
  objects** with `exc_type`, `exc_value`, `exc_notes`, `syntax_error`,
  `is_cause`, `frames[]` (each with `filename`, `lineno`, `name`,
  `locals`), `is_group`, `exceptions[]`. Documented example
  (`docs/api.rst`):

  ```json
  {"event": "Cannot compute!", "exception": [{"exc_type": "ZeroDivisionError",
   "exc_value": "division by zero", "frames": [{"filename": "...",
   "lineno": 2, "name": "<module>", "locals": {"var": "'spam'"}}], ...}]}
  ```

  Now `exception[0].exc_type: "RedfishAuthError"` is a query. The costs
  are real: the payload is much larger, and **`locals` are captured and
  serialized** — a frame holding a password variable puts it in the log.
  That interacts directly with §6 below.
- **`ExceptionRenderer(fn)`** — use when you want your own shape (e.g.
  type + message only, no frames), which is the middle ground that
  avoids `locals` while staying queryable.

**Recommendation for this repo:** keep `format_exc_info` for the console
(dev) renderer, and use `dict_tracebacks` only on the production JSON
path — but only after auditing what ends up in `locals`, because the
provider clients hold `password` in local scope
(`redfish/client.py`, `oneview/client.py`, `openmanage/client.py`).
Given that risk, `ExceptionRenderer` with a type+message+file:line
formatter is the lazier and safer upgrade. Doing nothing is defensible;
the current setup is not broken, only unqueryable.

### When logging an exception AND raising is double-reporting

The rule: **log it or propagate it, never both** — with one exception.

- You **handled** it (swallowed, degraded, continued): log it, at the
  level that describes the degradation, with the exception attached.
  This is the only place `logger.exception` belongs.
- You are **re-raising** (bare `raise`, or `raise X from exc`): do not
  log. Whoever finally handles it logs it, and they know more than you
  do. A log-and-raise pair produces two records of one event, at
  different levels, in different files, with the same traceback — and
  the on-call has to work out they are the same incident.
- The one legitimate log-and-raise: you are **adding information the
  caller cannot see** and the caller genuinely cannot reconstruct it
  (a request id, a row number, a raw payload you are about to drop).
  Log it at DEBUG or INFO as *data*, then raise. Do not log a
  traceback twice.

**This repo does not have a log-and-raise problem.** Every
`logger.exception` call site (`ingest.py:317`, `run_collector.py:897`,
`run_collector.py:965`, `ucs_central/provider.py:430`) genuinely
terminates the exception — `continue`, `return None`, `return []`,
`return 1`. That is the correct pattern and worth keeping.

---

## 4. Cardinality — logs vs Prometheus labels

The two rules are related but **not** the same, and conflating them is a
common way to break a Prometheus server.

### Prometheus labels: the hard rule

Every distinct label-value combination is a **new time series**, held in
memory and on disk for the retention window, and it never goes away when
the value stops being produced (`prometheus_client` — a `Counter` with
`labelnames` creates a child per `.labels(...)` call and keeps it for the
process lifetime; a child is only removed by an explicit `.remove()`).
So a label value must be drawn from a **small, closed set fixed at code
time**: HTTP method, status class, route template, outcome enum, vendor,
manager type. Never: a server id, a hostname, a serial, a URL path
supplied by a caller, an error message, a timestamp, a user id.

### Log fields: the soft rule

A log line is a document. High-cardinality fields are the *point* — a
`host`, a `serial`, an `external_id` on a log line is how you find the
one server that failed. What actually costs money in a log index is:

- **a field whose name is unbounded** (`{server_id}_status: "ok"` mints
  a new index field per server — this repo does none of this);
- **a large value repeated on every line** (a full payload, a list of
  4,000 hosts);
- **volume**, which is the rate rule from §2, not a cardinality rule.

So: high-cardinality *values* in logs are fine and desirable;
high-cardinality *keys* are not; and neither is fine in a metric label.

**The bridge rule:** if you want to alert on it, it is a metric with
bounded labels. If you want to find the specific instance afterwards, it
is a log field with the unbounded identifier. Emit both — the metric
tells you *that* 40 hosts are failing, the log tells you *which*.

### Findings here

- `redfish.credential_circuit_open` logs `hosts=sorted(...)`, and
  `ucs_central.profiles_in_unregistered_domain` logs `domains=unmatched`
  — lists. Fine for a log (bounded by the failure being rare, and the
  list is the actionable part). Would be fatal as a metric label.
- **`http_requests_total` has a real unbounded-label bug.**
  `backend/app/main.py:120`:
  ```python
  route = request.scope.get("route")
  path_label = route.path if route is not None else request.url.path
  ```
  `route.path` is the template (`/api/v1/servers/{server_id}`) — correct,
  bounded. But `route is None` is exactly the **404** case, and then the
  raw caller-supplied path becomes a label value. Every scan for
  `/wp-login.php`, `/.env`, `/actuator` permanently mints a time series
  in `http_requests_total` *and* a full histogram in
  `http_request_duration_seconds` (which is far worse — a histogram
  child is one series per bucket). On an internet-reachable Route this
  is an unbounded memory leak in the Prometheus server. The fix is one
  line: `else "<unmatched>"`.

---

## 5. Logging vs the Prometheus metrics already exposed

`backend/app/observability/metrics.py` defines four:
`http_requests_total`, `http_request_duration_seconds`,
`mongo_ping_failures_total`, `cache_operations_total`.

### What goes where

| Question | Answer |
|---|---|
| "How often / how many / how long?" | **metric**. Aggregation, rates and alerts are what a counter is for. |
| "What exactly happened to *this* one?" | **log**, with the identifier. |
| "Is this failing right now, and which ones?" | **both** — a counter to alert on, a log line to identify. |

**The anti-pattern:** deriving an alert from a log grep when a counter
exists — or worse, when one could. A log-based alert costs a full-text
query per evaluation, breaks the moment someone rewords the event name,
silently reports zero when the log pipeline is down (indistinguishable
from healthy), and cannot express a rate over a window without the log
backend re-implementing Prometheus badly. Rule: **if you would ever
alert on it, it is a counter first and a log line second.**

### Findings here

- **`mongo_ping_failures_total` is defined and never incremented.**
  Grepped across `backend/` and `tools/`: the only occurrences are its
  own definition (`metrics.py:25-28`) and the import line. The one place
  that should increment it — `mongodb/client.py:83`, the `except
  PyMongoError` that makes `/health/ready` return 503 — logs a warning
  and nothing else. So today the *only* way to alert on "readiness is
  flapping" is to grep for `mongo.ping_failed`, which is precisely the
  anti-pattern, and it is caused by a dead metric rather than a design
  choice. Fix: one `.inc()`.
- **Nothing about collection runs is a metric at all.** This is the
  known gap (CLAUDE.md's not-done item 0: staleness detection). Worth
  writing down here because it is a logging question too: the collector
  emits excellent structured summaries (`redfish.run_summary`,
  `intersight.run_summary`, `ucs_central.domain_summary`,
  `collector.partial_run`) and *none of them is queryable as a rate*. A
  CronJob pod is never scraped, so the collector cannot export its own
  metrics — the correct place is the API deriving gauges from Mongo's
  `last_seen_at`. Until then, the run summaries are the only signal and
  they can only be grepped. **The logs are not the bug here; the missing
  metric is.**
- `cache_operations_total` is the model to copy — every branch in
  `redis/cache.py` increments the counter *and* logs the key. Counter to
  alert on, `key=` to investigate with. Correct.

---

## 6. Secret hygiene: assessing `_drop_sensitive_keys`

```python
_SENSITIVE_KEYS = frozenset(
    {"password", "token", "authorization", "secret", "api_key", "credential"}
)

def _drop_sensitive_keys(_logger, _method_name, event_dict):
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            del event_dict[key]
    return event_dict
```

The module docstring already frames it correctly — "the last line of
defense if a value with one of those names is ever passed by mistake" —
and that framing is the most important thing about it. It is not a
redactor and does not claim to be.

**What it catches:** a top-level event-dict key whose name is an exact
case-insensitive member of the set. `logger.info("x",
password=pw)` → gone. That is a real class of mistake and it is worth
having.

**What it cannot catch, in rough order of likelihood here:**

1. **Nested payloads.** It iterates only top-level keys.
   `logger.info("x", connection={"username": u, "password": p})` passes
   straight through, and so would any `.model_dump()` of a settings or
   connection object. Making it recursive is ~5 lines and would close
   the largest gap.
2. **Exception messages.** The chain is ordered
   `_drop_sensitive_keys` → `StackInfoRenderer` → `format_exc_info`
   (`config.py:57-59`), so the traceback string is produced *after* the
   scrub and is never examined. Neither is `error=str(exc)`, which the
   repo passes at a dozen sites (`redfish/client.py:252`,
   `oneview/client.py:281`, `openmanage/client.py:165`,
   `ucs_manager/client.py:124`, `ucs_central/client.py:150`, every
   `cache.*_failed`). An `httpx` error message contains the request URL;
   a vendor SDK's message can contain whatever it was handed.
3. **URLs with embedded credentials.** `https://user:pw@host/…` under a
   key named `endpoint` or `url` is invisible to a name-based filter.
   This is a live risk — `INVENTORY_*_IP` values are operator-supplied
   and `endpoint=` is logged on almost every provider line.
4. **A `repr()` of a client object.** Any object whose `__repr__`
   includes its auth config, logged under an innocuous key, or captured
   in a `locals` frame if the chain is ever switched to
   `dict_tracebacks` (§3).
5. **Near-miss key names.** `api_key` is in the set; `apikey`,
   `api-key`, `access_token`, `bearer`, `pem`, `private_key`, `passwd`,
   `pwd` are not. `INVENTORY_INTERSIGHT_API_KEY_PEM` is the live example
   — a key named `api_key_pem` would not match.

**Where the repo does better than the processor.** The real defence here
is at the call sites, and it is deliberate.
`redfish/client.py:545-558` excludes the session-establishment exchange
from HTTP debug logging outright rather than redacting it, with the
reasoning written down: "that one request carries the password and its
response carries the token, and a redactor that must be perfect is a
worse design than never formatting the value at all." That is exactly
right, and it is the reason the processor's weaknesses have not bitten.
`intersight/client.py:297` follows the same discipline (method, path,
status only — never a header). No log call site in
`backend/app` or `tools/` logs a password, a token, a header dict or a
request body.

**Assessment:** correct as a backstop, correctly documented as one, and
carrying more weight than the docstring implies only in case (1). The
two cheap improvements are making it recursive and adding
`access_token`/`private_key`/`pem`/`passwd`/`apikey` to the set. Do not
make it try to scrub free text — a regex over exception messages is the
"redactor that must be perfect" the repo already rejected.

---

## 7. Performance

### Where the cost is

From `docs/performance.md`
(https://github.com/hynek/structlog/blob/main/docs/performance.md), and
confirmed in `_config.py` / `stdlib.py`:

- **`cache_logger_on_first_use=True`** (set here, `config.py:69`) makes
  `BoundLoggerLazyProxy.bind` replace itself with a closure over the
  already-assembled logger:
  ```python
  def finalized_bind(**new_values):
      if new_values:
          return logger.bind(**new_values)
      return logger
  if self._cache_logger_on_first_use is True or ...:
      self.bind = finalized_bind
  ```
  This is the right setting for a service, and it has one consequence
  worth knowing: **a `configure()` call after a proxy's first use does
  not reach that proxy** — the closure captured the old processors. In
  production this is harmless (`configure_logging` runs once). In tests
  that drive the lifespan more than once with a different `environment`,
  the second configuration silently does nothing to already-used
  module-level loggers.
- **`structlog.stdlib.BoundLogger` vs the native filtering logger.** The
  docs recommend the native `make_filtering_bound_logger` for
  performance because it filters by level *before* doing any work. This
  repo cannot use it: it deliberately routes stdlib records (uvicorn,
  PyMongo) through `ProcessorFormatter`, which requires the stdlib
  wrapper. That is the correct trade and the module docstring explains
  it — one log shape beats a few microseconds.
- **Consequence: level filtering happens too late.**
  `stdlib.BoundLogger._proxy_to_logger` runs `_process_event` — i.e. the
  *whole* processor chain — and only then calls the stdlib method, where
  the handler's level check finally drops it. So today a `logger.debug()`
  at `INFO` still pays `merge_contextvars`, `add_log_level`,
  `add_logger_name`, `TimeStamper`, `_drop_sensitive_keys`,
  `StackInfoRenderer` and `format_exc_info` before being thrown away.
  structlog ships the fix and documents it in its own source
  (`stdlib.py`, `filter_by_level`): *"Should be the first processor if
  stdlib's filtering by level is used so possibly expensive processors
  like exception formatters are avoided in the first place."* Adding
  `structlog.stdlib.filter_by_level` as the first entry of
  `structlog.configure(processors=[...])` — **not** of
  `foreign_pre_chain`, since stdlib records have already passed their own
  level check — is a one-line change.

### Is logging on a hot path here?

**No, and that is worth stating so nobody optimizes it.** Counted across
`backend/app` and `tools`: **84 log calls — 1 DEBUG, 36 INFO, 35
WARNING, 8 ERROR, 4 `exception`, 0 CRITICAL.** Nothing logs per server
in the success path. The two per-item calls are
`ingest.server_failed` (`ingest.py:317`, error path only) and
`redfish.gpu_baseboard_merged` (`redfish/provider.py:477`, per GPU tray,
rare). The API logs one line per request. At 10,000 servers a full
collector run emits on the order of tens of lines.

Therefore: `filter_by_level` is worth adding for correctness of design,
not for measured speed, and nothing else about the chain needs tuning.

---

## 8. `backend/app/infrastructure/logging/config.py` — assessment

**The user is right that this file is good.** Concretely, what it gets
right and why it matters:

1. **One pipeline, not two.** Routing stdlib records through
   `ProcessorFormatter` with a `foreign_pre_chain` — rather than
   configuring stdlib separately — is structlog's own documented
   integration and it is the single decision that stops uvicorn and
   PyMongo emitting a second, differently-shaped log format into the
   same stream. The docstring names this failure mode ("the 'dual
   pipeline' mistake"), which is the sort of thing that stops a future
   session from "simplifying" it.
2. **`merge_contextvars` is first.** Required for §1 to work at all, and
   correctly placed ahead of everything that could drop the event.
3. **`shared_processors` is reused as `foreign_pre_chain`**, so a
   PyMongo warning gets the same timestamp format, the same level key
   and the same secret scrub as an application line. This is the part
   most integrations get wrong by copying only half the list.
4. **`remove_processors_meta` before the renderer** — required, and
   easy to omit; without it the internal `_record`/`_from_structlog`
   keys leak into the JSON.
5. **`utc=True` on `TimeStamper`.** Not a detail: a container whose TZ
   differs from the cluster's makes correlation across pods guesswork.
6. **Renderer chosen by environment, one call site.** Dev gets
   `ConsoleRenderer`, production gets `JSONRenderer`, from the same log
   calls.
7. **`uvicorn.access` silenced at `:97`** with the reason written down —
   the repo's own `request.completed` line supersedes it and carries
   `request_id` and `duration_ms`, which the access log does not.
8. **`root_logger.handlers = [handler]`** — replacement, not `addHandler`.
   Appending is how services end up double-logging every line after a
   library calls `basicConfig()`.
9. **`cache_logger_on_first_use=True`** — correct for a service (§7).

**What is wrong with it, in priority order:**

| # | Issue | Fix |
|---|---|---|
| 1 | `structlog.stdlib.filter_by_level` is missing, so below-level events pay the whole processor chain before being dropped (§7). | Add it as the first entry of `structlog.configure(processors=…)` only. |
| 2 | `_drop_sensitive_keys` is top-level-only, so a nested dict or a `model_dump()` passes through (§6). | Make it recurse; extend the key set. |
| 3 | `_drop_sensitive_keys` runs *before* `format_exc_info`, so nothing scrubs the traceback (§6). | Accept it — but know it, and re-evaluate if switching to `dict_tracebacks`, which serializes frame `locals`. |
| 4 | `format_exc_info` puts the traceback in JSON as one escaped string, so `exc_type` is not queryable (§3). | Optional: `dict_tracebacks` or a custom `ExceptionRenderer` on the production path only. |
| 5 | No `structlog.processors.add_log_level_number`, and `service`/`environment` are stamped by an inline `lambda`. | Cosmetic. The lambda is fine; a named function would satisfy the repo's docstring convention. |
| 6 | `configure_logging` is called inside `lifespan` (`main.py:55`), which runs *after* uvicorn has already emitted its own startup lines through the default root handler. | Minor. Call it before `uvicorn.run`/at import of the ASGI factory if the first three lines of a pod's log matter. |
| 7 | No docstrings on `_drop_sensitive_keys`, contrary to CLAUDE.md convention 8. | Add one. |

None of 1–7 is a defect that produces wrong output today. Items 1 and 2
are the two worth doing.

---

## 9. Call-site assessment

84 log calls: 1 DEBUG / 36 INFO / 35 WARNING / 8 ERROR / 4 `exception` /
0 CRITICAL. No f-string or `%`-format interpolation anywhere in a log
call — grepped for `.info(f"` and friends across `backend/app` and
`tools`, **zero hits**. Every call uses a dotted event name plus keyword
fields. That is better than most production codebases and the report
below should be read against that baseline.

Ordered worst-first.

| # | Site | Problem | Fix |
|---|---|---|---|
| 1 | `backend/app/infrastructure/providers/oneview/provider.py:356` | `except Exception: return uri, None` — the exception is **discarded entirely**, with no log at any level. The only survivor is an aggregate count at `:373` (`oneview.power_supplies_unreadable`, `servers=failures`). A 401, a timeout, a JSON decode error and a typo in the URI are indistinguishable, and PSU data silently becomes `None` for the whole fleet. This runs under `asyncio.gather` at `:365`, so a bug affects every server at once. | `except Exception as exc:` + `logger.warning("oneview.psus_unreadable", uri=uri, error=str(exc))` before returning, or at minimum tally `type(exc).__name__` into the aggregate line. |
| 2 | `backend/app/infrastructure/mongodb/client.py:83` | `logger.warning("mongo.ping_failed")` carries **no detail at all** — no `error=`, no `exc_info`, and the `PyMongoError` is caught and dropped. This is the line that makes `/health/ready` return 503, so it is the single most operationally important warning in the API, and it says nothing. Compounding it, `mongo_ping_failures_total` (`observability/metrics.py:25`) is **defined and never incremented anywhere in the repo** — so the only way to alert on readiness flapping is to grep this contentless line. | `except PyMongoError as exc:` → `mongo_ping_failures_total.inc()` then `logger.warning("mongo.ping_failed", error=str(exc))`. Two lines; closes both the log gap and the dead-metric gap. |
| 3 | `tools/run_collector.py:907` (and every collector log line downstream) | **No run-scoped context is bound anywhere.** `configure_logging` is called and nothing else. Consequences: `ingest.completed` (`ingest.py:330`) carries `fetched/created/updated/errors` and **no `manager_type`** — with five CronJobs writing to one log stream, you cannot tell which collector's run a summary belongs to; `intersight.subresource_failed` (`intersight/provider.py:358`) has no endpoint; there is no `run_id` to group a run's lines by; and each provider re-passes `endpoint=`/`manager_id=` by hand at ~40 sites, inconsistently. | `clear_contextvars()` + `bind_contextvars(run_id=…, manager_type=…, endpoint=…)` right after `configure_logging` (§1). Tasks created afterwards inherit it; `asyncio.to_thread` carries it into the SDK threads. |
| 4 | `backend/app/main.py:120` | `path_label = route.path if route is not None else request.url.path` — the `None` branch is the 404 case, so a caller-supplied path becomes a Prometheus label value on both `http_requests_total` and `http_request_duration_seconds` (§4). Unbounded series growth from any scanner. | `else "<unmatched>"`. |
| 5 | `backend/app/infrastructure/providers/intersight/client.py:297` | An HTTP request trace logged at **INFO**, gated by a `_debug_http` boolean. `redfish/client.py:558` does the identical thing at DEBUG. Two files, same event class, two levels — and a boolean flag doing a job the level exists for. Per §2 rule 2, anything at HTTP-request rate is DEBUG. | `logger.debug(...)`; keep the flag or drop it in favour of `LOG_LEVEL=DEBUG`. |
| 6 | `redfish/provider.py:259` vs `intersight/provider.py:550` | The **same condition at two levels**: `redfish.run_budget_exceeded` is ERROR, `intersight.run_budget_exhausted` is WARNING. Both mean "this run did not see the whole fleet" and both feed `collection_errors` → exit code 3. One will page and one will not. | Make `intersight.run_budget_exhausted` ERROR, matching §2's collector rule, and add the `hint=` the Redfish one has. |
| 7 | `ucs_central/provider.py:162` vs `:368` | A domain with no address is logged **twice at two levels**: `ucs_central.domain_without_address` at WARNING (`:162`) and `ucs_central.domain_skipped` with `reason="no address"` at INFO (`:368`). The code comment at `:361` says this case "is a fault, not a pruning decision", which the INFO line contradicts. | Keep one. If `domain_skipped` must cover both reasons, choose its level from the reason, or emit the no-address case only from `:162`. |
| 8 | `redis/client.py:48` | `logger.warning("redis.connect_failed_at_startup")` — the `RedisError` is caught and its message dropped. WARNING is the right *level* (Redis is deliberately non-fatal, and the comment says so), but "cannot reach Redis" with no reason means the operator cannot tell DNS from auth from TLS. | `except RedisError as exc:` → add `error=str(exc)`. |
| 9 | `redfish/provider.py:339` | `redfish.tls_verification_disabled` is WARNING and fires **once per host**, inside `_collect_host`. On a fleet where verification is off, a 400-host run emits 400 identical warnings and buries the run's real output. `oneview/provider.py:280` explicitly solves this same problem by aggregating ("One aggregated line, not one per host"). | Aggregate: count the hosts and emit one warning with `hosts=n` from `_log_summary`, following the OneView precedent. |
| 10 | `openmanage/provider.py:192` | `ome.no_matching_profiles` at INFO with only `endpoint=`. "The appliance returned zero servers" is exactly the ambiguous case that `redfish._log_summary` (`:297`) and `collector.name_filter_applied` (`run_collector.py:505`) both go out of their way to disambiguate — an empty estate and a wrong name pattern look identical here. | Add `profiles=`, `devices=` and `name_pattern=` so the zero is attributable; consider WARNING, since a collector that collected nothing is nearly always a misconfiguration. |
| 11 | `oneview/provider.py:330` (`oneview.power_supply_source`) | INFO, emitted unconditionally with `collect_psus=` — a configuration echo rather than an event. Harmless (once per run) but it is the shape that grows into per-item INFO noise. | Fold into the run summary. |

### Call sites that are right, and why (do not "fix" these)

- **`redfish/provider.py:297` `_log_summary`** — one summary per run,
  emitted *unconditionally including the all-zero case*, with the
  reasoning in the docstring: "0 collected, 0 failed" is an empty
  inventory, "0 collected, 400 failed" is a credential fault, and
  without the line they are identical. Same discipline in
  `run_collector.py:505` and `ucs_central/provider.py:351`. This is the
  best logging in the repo.
- **Every `logger.exception` call site** (`ingest.py:317`,
  `run_collector.py:897,965`, `ucs_central/provider.py:430`) terminates
  its exception rather than re-raising — no double-reporting (§3).
- **`exception_handlers.py:60,80,105`** — client-caused errors at INFO,
  only the unhandled case at ERROR (`:121`, with `exc_info=exc`). §2's
  "a 4xx is never ERROR" rule, applied correctly.
- **`request_context.py:58-66`** — `bound_contextvars` around the whole
  downstream call, one `request.completed` line, level chosen from the
  status code.
- **`redis/cache.py`** — every branch increments a bounded-label counter
  *and* logs the unbounded key. The metric/log split from §5, done right.
- **`redfish/client.py:545-558`** — session exchange excluded from HTTP
  logging outright rather than redacted (§6). The best security decision
  in the logging code.
- **`oneview/provider.py:280` and `:373`** — deliberate aggregation of a
  per-host condition into one line, with the reason written down.
- **`oneview/client.py:356` `oneview.collection_truncated` at ERROR** —
  correctly reasoned in-comment: a silently truncated collection is
  indistinguishable from a healthy run against a smaller fleet.
- **`hint=` fields** throughout (`redfish.run_budget_exceeded`,
  `oneview.subresources_unreadable`, `mongo.index_respecified`, …) —
  an operator-facing remediation string in a structured field, not
  prose in the event name. Unusual and good; keep it.

---

## 10. Summary of recommended changes

Not applied — this note changes no code.

**Do:**
1. `oneview/provider.py:356` — stop discarding the exception.
2. `mongodb/client.py:83` — attach `error=`, and increment the dead
   `mongo_ping_failures_total`.
3. `tools/run_collector.py:907` — bind `run_id` / `manager_type` /
   `endpoint` into contextvars for the run.
4. `main.py:120` — `else "<unmatched>"`, closing the Prometheus label leak.
5. `logging/config.py` — add `structlog.stdlib.filter_by_level` first;
   make `_drop_sensitive_keys` recursive and widen its key set.

**Consider:** levels 5–11 in the table above (the two run-budget
inconsistencies first, since they decide what pages).

**Do not:** switch to `make_filtering_bound_logger` (breaks the
single-pipeline design), add a regex redactor over exception text (the
repo already rejected that trade for a better reason), or optimize the
processor chain for speed (nothing here is on a hot path).
