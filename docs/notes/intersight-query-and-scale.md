# Intersight OData query syntax and fleet-scale collector design

Summary: Intersight's REST API is OData v4-flavored with the usual
`$filter`/`$select`/`$expand`/`$top`/`$skip`/`$orderby`/`$count`/`$apply`
query options, all exposed as SDK kwargs. **The design question is
answered yes**: this API is a small, fixed number of fleet-wide list
calls, not O(N) per server — either via client-side joins on the `Dn`
every sub-resource MO carries, or via `$expand` embedding sub-resources
directly into the parent list response. For 10,000 servers the join
plan below costs roughly **60–70 HTTP requests total**, against the
Redfish-standalone collector's ~25 *per BMC* (250,000 requests at the
same fleet size). Rate limits and 429 handling are **not documented
anywhere I could find** in Cisco's public sources — that's a real gap,
flagged below, not a fact I'm asserting either way.

## 1. OData query parameters — SDK kwargs and semantics

Every `get_<resource>_list` method in the installed SDK
(`intersight/api/compute_api.py:7591-7602`, and identically shaped in
`adapter_api.py`, `storage_api.py`, `management_api.py`, etc.) exposes
these kwargs, one-to-one with the query string options:

| SDK kwarg | Query option | Notes |
|---|---|---|
| `filter` | `$filter` | default `""` (unfiltered) |
| `orderby` | `$orderby` | no default |
| `top` | `$top` | **default 100** |
| `skip` | `$skip` | default 0 |
| `select` | `$select` | default `""` (all fields) |
| `expand` | `$expand` | no default |
| `apply` | `$apply` | aggregation/groupby |
| `count` | `$count` | bool |
| `inlinecount` | `$inlinecount` | default `"allpages"` |
| `at` | (versioning filter, not a plain OData option) | |
| `tags` | tag-usage summary | unrelated to paging |

Source: `intersight/api/compute_api.py:7591-7602` (`get_compute_physical_summary_list`), confirmed identical for `get_compute_server_setting_list` at `compute_api.py:8802-8813`.

**`$top` maximum is confirmed 1000**, not an assumption:
developer.cisco.com/docs/intersight/query-syntax/ states verbatim "The
maximum value for `$top` is 1000." Default when omitted is 100 (matches
the SDK docstring default independently).

`$filter` operators: `eq`, `ne`, `gt`, `lt`, `ge`, `le`, `and`, `or`,
`not`, `in`. String functions: `contains(s, subst)`, `startswith(s,
subst)`, `endswith(s, suffix)`, `tolower(s)`, `toupper(s)`. (Same page.)

## 2. THE DESIGN QUESTION — does this scale to 10,000 servers?

**Yes, two ways, and they can be combined.**

### Evidence for client-side join (the plan I recommend)

Every sub-resource MO I inspected in the installed SDK carries `Dn`,
`Parent` (a `MoBaseMoRelationship`), and `Ancestors`
(`[MoBaseMoRelationship]`) — the standard Intersight MO-hierarchy
fields, not something specific to one resource type:

- `adapter.HostEthInterface`: `intersight/model/adapter_host_eth_interface.py` attribute map includes `'dn': 'Dn'`, `'parent': 'Parent'`, `'ancestors': 'Ancestors'`, plus `'ep_dn': 'EpDn'` and `'vnic_dn': 'VnicDn'` (the parent server's DN embedded directly in the interface's own DN string, e.g. `sys/rack-unit-1/adapter-1/host-eth-1`).
- `storage.PhysicalDisk`: `intersight/model/storage_physical_disk.py:213-227` — same `Dn`/`Parent`/`Ancestors`/`RegisteredDevice` shape.
- `management.Controller`: `intersight/model/management_controller.py:213-227` — identical shape again.

So a sub-resource list response, fetched *once for the whole fleet*, can
be joined client-side to `compute.PhysicalSummary.Dn`/`.Moid` by
stripping the DN suffix (or matching `Ancestors`) — exactly the "one
`adapter.HostEthInterface` list call for ALL servers, joined on the
parent moref" shape the task asked me to prove or disprove. It's real:
these are ordinary flat OData collections, filterable/pageable like any
other, not implicitly scoped to one parent.

`compute.PhysicalSummary` itself is already a wide, pre-joined MO — it's
Intersight's own rollup view and already carries `NumAdaptors`,
`NumCpus`, `NumCpuCores`, `NumEthHostInterfaces`, `NumFcHostInterfaces`,
`AvailableMemory`, `Serial`, `Model`, `MgmtIpAddress`, `KvmIpAddresses`,
etc. directly (`intersight/model/compute_physical_summary.py`
attribute map) — so a fair amount of what a collector needs never
requires a join at all.

### Evidence for `$expand` also collapsing the fan-out

developer.cisco.com/docs/intersight/query-syntax/, `$expand` section,
quoted directly: *"The `$expand` query option specifies the related
resources to be included in line with retrieved resources"*; *"You can
expand all relationships within an MO. There is no limit on the
recursive expand you can use, but the api might time out if you have
too many expand"*; and nested/filtered expand is real syntax
(`Profiles($select=Name,ConfigResult%3B$expand=ConfigResult)`, semicolon
URL-encoded as `%3B` to separate the nested option list). So a single
`GET .../compute/PhysicalSummaries?$expand=...` can in principle embed
adapters/storage/management-controller inline in the same page, cutting
round trips further than the join plan — at the cost of heavier
per-page payloads and the documented, unbounded timeout risk on deep
expands. I did not find a documented cap on expanded collection size or
depth beyond that timeout warning — treat "how much expand fits in one
page before it times out" as something to load-test against a real
tenant, not something the docs settle numerically. **UNVERIFIED**: exact
expand paths available from `compute.PhysicalSummary` to
`adapter.HostEthInterface`/`storage.PhysicalDisk`/
`management.Controller` — the SDK models declare relationship fields per
MO but I did not enumerate which are `$expand`-able from
`PhysicalSummary` specifically vs. requiring a different root MO. What
would settle it: a live tenant call, or the downloadable OpenAPI YAML
(`cisco_intersight_1_0_11_20260320211357036.yaml`, referenced at the
bottom of developer.cisco.com/docs/intersight/intersight-api-reference-overview/)
searched for `x-relationship`/expand annotations on `PhysicalSummary`.

### Concrete request plan and count, 10,000 servers

Recommended: the join plan (predictable page cost, no expand-timeout
risk), `$top=1000` throughout, `$select` trimmed to only fields the
collector maps:

| Query | Est. rows | Pages @1000 |
|---|---|---|
| `GET compute/PhysicalSummaries?$select=Name,Serial,Model,Vendor,MgmtIpAddress,AvailableMemory,NumCpus,NumCpuCores,...&$orderby=Moid` | 10,000 | 10 |
| `GET adapter/HostEthInterfaces?$select=Dn,MacAddress,...&$orderby=Moid` | ~20,000 (2 NICs/server, adjust to real fleet) | 20 |
| `GET storage/PhysicalDisks?$select=Dn,Serial,Size,...&$orderby=Moid` | ~20,000 (2 drives/server) | 20 |
| `GET management/Controllers?$select=Dn,FirmwareVersion,...&$orderby=Moid` | ~10,000 (1/server) | 10 |

**Total: ~60 requests for 10,000 servers**, all fleet-wide, independent
of which servers exist — this does not grow per-server, only per
1000-row page. Compare the Redfish-standalone collector's ~25 round
trips *per BMC* (`docs/adr/0016`), i.e. 250,000 requests at the same
fleet size. If `$expand` proves reliable at scale (see UNVERIFIED
above), the `PhysicalSummaries` call alone could subsume some or all of
the other three, pushing the count toward ~10-15 requests — but the
join plan alone already clears the 10k target by roughly 3–4 orders of
magnitude versus Redfish, so I would not block a first collector
implementation on proving expand out first.

## 3. Expand depth / size limits

No documented numeric cap. Only the timeout warning quoted above ("the
api might time out if you have too many expand"). `$select` is
explicitly recommended alongside `$expand` to shrink payload size:
"Use `$select` with `$expand` to reduce the response body size to get
the required fields" (same page). Expanded *collections* nested under
`$expand` can themselves be restricted with `$top`/`$skip`/`$orderby`
("If the expanded item is a collection, you can restrict your
collection using `$top`, `$skip` and `$orderby`") — so an expanded
child collection is not automatically capped by the parent's own `$top`;
each needs its own limit if you don't want it unbounded. **UNVERIFIED**:
whether an expanded child collection has its own separate default cap
(likely the same default-100 as top-level lists, but I found no text
saying so explicitly) — settle by an empirical call against a server
with more mapped children than 100.

## 4. Rate limits and throttling

**Not documented in any Cisco primary source I could reach.** I fetched
developer.cisco.com/docs/intersight/query-syntax/,
developer.cisco.com/docs/intersight/ (index),
developer.cisco.com/docs/intersight/intersight-api-reference-overview/,
developer.cisco.com/docs/intersight/api-getting-started/ (404 — that
path doesn't exist), intersight.com/apidocs/introduction/overview/, and
web-searched for "rate limit"/"429"/"throttl" scoped to both domains.
None returned rate-limit numbers, a 429 status contract, or
`Retry-After`/`X-RateLimit-*` header names. This is a real
**UNVERIFIED / documentation gap**, not a "no limits exist" claim — Cisco
Meraki (a sibling DevNet property) publishes explicit numeric limits, so
the absence here looks like Intersight simply doesn't publish them
rather than there being none. What would settle it: provoke a real 429
against a live tenant and read the response headers, or open a Cisco
TAC/DevNet support ticket asking directly.

**What the SDK itself does — checked directly, not assumed:**
`intersight/rest.py:102-158` (`RESTClientObject.request`) contains
**zero application-level retry or backoff logic**. It makes one
`urllib3` request, and on any non-2xx status raises a typed exception
(`UnauthorizedException`/`ForbiddenException`/`NotFoundException` for
401/403/404, `ServiceException` for 5xx, generic `ApiException`
otherwise — `rest.py:220-234`) with `.status`, `.reason`, `.body`,
`.headers` populated from the HTTP response
(`intersight/exceptions.py`, `ApiException.__init__`). A 429 would fall
into the generic `ApiException` branch (not 401/403/404/5xx) and simply
raise — no automatic retry, no `Retry-After` parsing, no backoff.

The only retry surface is `Configuration.retries`
(`intersight/configuration.py:259-260`, comment: *"Adding retries to
override urllib3 default value 3"*) — if left `None` (the default),
`rest.py:65-66` skips passing it to the `urllib3.PoolManager` at all, so
`urllib3`'s own default `Retry` policy applies. `urllib3`'s default
`Retry` object retries on connection-level failures (DNS/connect/read
errors) but has no `status_forcelist` by default — it does **not** retry
on HTTP status codes including 429 unless the caller explicitly
constructs a `urllib3.util.Retry(status_forcelist=[429, 503, ...])` and
passes it as `Configuration.retries`. **Conclusion: a collector must
implement its own 429/backoff handling** (e.g. via `Configuration.retries`
set to a `urllib3.Retry` with `status_forcelist`, or an explicit retry
wrapper around each SDK call) — the SDK will not do this for you, and
the vendor's public docs give no numbers to tune it against.

## 5. Paging

No documented next-page token/link — `$top`/`$skip` (offset paging) is
the only mechanism surfaced anywhere I found, matching the SDK kwargs in
§1. No Intersight-specific text on stability under concurrent
modification, nor an explicit recommendation to `$orderby=Moid`. That
absence matters: `$skip`-based offset paging without a stable
`$orderby` is generically vulnerable to skipped/duplicated rows if MOs
are inserted/deleted between page fetches, and `docs/adr/0006` in this
repo already burned time on a paging-correctness bug elsewhere in this
project for a related reason (string-vs-native date comparison, not
this exact issue, but the same family of "silent wrong results"). I did
find `$orderby=Serial` used in a worked example
(`developer.cisco.com/docs/intersight/query-syntax/`, via search
excerpt: `$top=2&$select=Model,Serial&...&$orderby=Serial`), which shows
`$orderby` is idiomatic alongside `$top`/`$skip`, but that's an example
choosing a domain-relevant sort key, not a documented paging-stability
recommendation. **Recommendation, not a documented fact**: always pair
`$skip`/`$top` with `$orderby=Moid` (Moid is immutable and always
present, `intersight/api/compute_api.py:7592` confirms `Moid` is
returned regardless of `$select`) for a stable sort key across pages.
Mark this UNVERIFIED as vendor guidance; treat it as prudent default
practice instead.

## 6. Server-side name filtering

Yes: `startswith(Name, 'ocp')` is exactly the documented `startswith`
syntax (§1, `$filter` string functions), e.g.
`$filter=startswith(Name,'ocp')` on `compute.PhysicalSummary`. `contains`,
`endswith`, `tolower`/`toupper` are also available for prefix/substring
variants.

**This is bandwidth-only, never the correctness boundary** — per this
repo's convention (`CLAUDE.md`'s collector-architecture section), the
`_NameFilteredProvider` wrapper in `tools/run_collector.py` applies
`INVENTORY_COLLECTOR_NAME_PATTERN` client-side regardless of what the
vendor API can pre-filter, exactly as it already does for every other
collector. A server-side `$filter=startswith(Name,'ocp')` only reduces
what crosses the wire; it must not replace the existing client-side
filter, both to keep one code path for the pattern and because Intersight's
`startswith` semantics (plain prefix match) don't necessarily match
whatever regex `INVENTORY_COLLECTOR_NAME_PATTERN` expresses in the
general case.

## 7. Organization scoping

**Not available on the physical-inventory MOs a collector reads.**
Checked the SDK models directly: `Organization` (an
`OrganizationOrganizationRelationship`) exists on **configuration**
MOs — `server_profile.py:283,366` and `bios_policy.py:3719,4223` both
declare it — but I grepped for the same field on
`compute_rack_unit.py`, `compute_blade.py`, `compute_server_setting.py`
and found **no** `'organization':` key in any of them (empty grep
results). `compute.PhysicalSummary`'s attribute map (§ above) has no
`Organization` field either. Physical inventory (`compute.*`,
`adapter.*`, `storage.*`, `management.*` — the read-only discovered MOs
this collector would query) is scoped by account/domain-group, not by
Organization; Organization scoping is a policy/profile-assignment
concept, not an inventory one. This matters for the stated reason: it
rules out "scope the Intersight collector's queries to one Organization"
as a way to avoid overlap with the existing UCS Central collector —
there's no Organization axis on the inventory data to scope by in the
first place. Any UCS-Central/Intersight de-duplication would have to
happen on `(vendor, serial_normalized)` as `IngestService` already does
for every provider (per `CLAUDE.md`), not on an Intersight-side filter.

## 8. Errors

Structure confirmed directly from the SDK (not the vendor's prose docs,
which I did not find covering this): `ApiException`
(`intersight/exceptions.py`) exposes `.status` (int HTTP status),
`.reason` (HTTP reason phrase), `.body` (raw response bytes/str — the
JSON error body Intersight returns, unparsed by the SDK), and `.headers`
(the full response header list). The SDK does **not** parse `.body`
into a typed error object — a caller gets raw bytes and must
`json.loads` it themselves to get at whatever Intersight's own error
schema is. **UNVERIFIED**: the actual JSON shape of that error body
(field names for an error code/message) — I did not find a documented
schema and did not have a live tenant to provoke a real error against.
What would settle it: a live 4xx/5xx call, or the OpenAPI YAML's error
response schema for a representative endpoint.

## Open questions / UNVERIFIED

1. **`$expand` paths from `compute.PhysicalSummary` to adapter/storage/management children** — whether they're directly expandable from that MO or require a different root. Settle via a live tenant call or the OpenAPI YAML's relationship annotations.
2. **Rate limits, 429 contract, and retry headers** — undocumented anywhere I could reach; settle by provoking a real 429 against a live tenant, or a direct Cisco TAC/DevNet support question.
3. **Expanded child-collection default page size** — likely the same default-100 as top-level `$top`, not stated explicitly anywhere found. Settle empirically.
4. **Paging stability under concurrent modification** — no Intersight-specific text found; `$orderby=Moid` is a prudent default, not documented vendor guidance. Settle by asking Cisco directly, or accept the prudent default without further verification (low risk, cheap to apply).
5. **Error body JSON schema** — SDK returns raw bytes; the field-level shape (error code, message) is unverified. Settle via a live error response or the OpenAPI YAML.
6. **Real per-server sub-resource counts** (NICs/drives/mgmt-controllers per server) used above are estimates (2/2/1) for the request-count table — the real ratios depend on the actual fleet's hardware mix and should be pulled from a live tenant's `NumEthHostInterfaces`/`NumAdaptors` etc. on `compute.PhysicalSummary` before finalizing a page-count budget.
