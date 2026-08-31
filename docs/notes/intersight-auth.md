# Cisco Intersight API authentication — research notes

Primary sources: the official `intersight` Python SDK wheel, version
`1.0.11.2026072720` (files cited as `intersight/<file>:<line>`, read from
the extracted wheel), its bundled README
(`intersight-1.0.11.2026072720.dist-info/METADATA`), and the web sources
listed inline. Anything not confirmed by a primary source is marked
`UNVERIFIED` with what would settle it.

## 1. HTTP Signature scheme

Confirmed `hs2019` (`intersight/signing.py:59` — `SCHEME_HS2019 = 'hs2019'`),
alongside the deprecated `rsa-sha256`/`rsa-sha512`. The SDK's own
`HttpSigningConfiguration` docstring explicitly tells callers to avoid the
older two (`intersight/signing.py:87-91`).

**The required-header set is smaller than the assumption in the task
brief.** The SDK's own official auth example (README §2,
`METADATA:127-146`) signs exactly:

```
signed_headers=[
    intersight.signing.HEADER_REQUEST_TARGET,
    intersight.signing.HEADER_HOST,
    intersight.signing.HEADER_DATE,
    intersight.signing.HEADER_DIGEST,
]
```

i.e. `(request-target)`, `Host`, `Date`, `Digest` — **not** `(created)`.
A Cisco Community thread on a real 401 from this exact SDK independently
states "the minimum required headers for HTTP signing in Intersight are
`(request-target)`, `Host`, `Date`, and `Digest`"
([community.cisco.com](https://community.cisco.com/t5/cisco-intersight/unauthorized-error-when-connecting-to-intersight-applicance/td-p/4110365)).
So: build the collector's signer on that four-header set, not on
`(created)`/`(expires)`.

`(created)` and `(expires)` are still valid signed-header choices — the
class defaults to `[HEADER_CREATED]` alone when `signed_headers` is
omitted (`intersight/signing.py:180-182`), and using `(expires)` requires
`signature_max_validity` to be set or the constructor raises
(`intersight/signing.py:177-179`). `Content-Type` is not a named constant
in `signing.py` at all; it's not in the special-cased header list
(`_get_signed_header_info`, `intersight/signing.py:277-306`), so it can
only be signed as an arbitrary extra header pulled from the `headers`
dict passed in — not a value Intersight requires or that the SDK
special-cases.

The signing string is built in
`HttpSigningConfiguration._get_signed_header_info` /
`get_http_signature_headers` (`intersight/signing.py:255-402`): for each
name in `signed_headers`, it resolves the value (computing
`(request-target)`, `(created)`, `(expires)`, `Host`, `Date`, `Digest`
itself; anything else is looked up case-insensitively in the caller's
`headers` dict or the call raises), joins `"name: value"` lines with
`\n`, hashes that string, and signs the hash
(`_get_message_digest` / `_sign_digest`, `intersight/signing.py:378-421`).
The `Authorization` header is assembled in `_get_authorization_header`
(`intersight/signing.py:423-433`) as
`Signature keyId="...",algorithm="hs2019",created=...,headers="...",signature="..."`.

The class is called `HttpSigningConfiguration`
(`intersight/signing.py:73`) — confirmed by reading the source, not
assumed from the task brief.

## 2. API key v2 vs v3

Confirmed against the SDK's own constants and its own README example
(`METADATA:118-146`), which detects the version by regexing the PEM body:

```python
if re.search('BEGIN RSA PRIVATE KEY', api_key):
    signing_algorithm = intersight.signing.ALGORITHM_RSASSA_PKCS1v15   # v2
elif re.search('BEGIN EC PRIVATE KEY', api_key):
    signing_algorithm = intersight.signing.ALGORITHM_ECDSA_MODE_DETERMINISTIC_RFC6979  # v3
```

Both constants exist verbatim in `intersight/signing.py:53-54`:
`ALGORITHM_RSASSA_PKCS1v15 = 'RSASSA-PKCS1-v1_5'` and
`ALGORITHM_ECDSA_MODE_DETERMINISTIC_RFC6979 = 'deterministic-rfc6979'`.
The key-loading code (`_load_private_key`,
`intersight/signing.py:229-263`) branches purely on the PEM
pre-encapsulation boundary text (`RSA PRIVATE KEY` vs `EC PRIVATE KEY`
vs PKCS8 `PRIVATE KEY`/`ENCRYPTED PRIVATE KEY`, the last resolved by OID)
— it does not require the caller to declare v2/v3 up front; only
`signing_algorithm`, if the caller sets it explicitly, is validated for
compatibility with whatever key type was actually loaded
(`intersight/signing.py:255-263`, raises `"Signing algorithm {0} is not
compatible with private key"` on mismatch).

**Which version Intersight issues for new keys today is UNVERIFIED from
these sources** — none of `developer.cisco.com/docs/intersight/authentication/`,
the SDK README, or the community threads fetched state a current default.
What the SDK guarantees: `signing_algorithm` is optional and can be left
`None`, and the code will infer a sane default from the key type alone
(RSA → `RSASSA-PSS`, EC → `fips-186-3`; see `_sign_digest`,
`intersight/signing.py:404-421`) — so a collector does **not** need to
hardcode v2 vs v3 support as a branch; detecting the PEM header (as the
SDK's own example does) and letting the SDK pick the matching default
signing algorithm handles both uniformly. Confirm the currently-issued
default empirically by generating one key from the Intersight portal
against the target account and reading its PEM header — that is the only
way to settle this without an authoritative, dated Cisco statement.

**Failure mode for a mismatched algorithm is local, not a server round
trip**: `HttpSigningConfiguration.__init__` raises
`Exception("Signing algorithm {0} is not compatible with private key")`
before any HTTP request is sent, if `signing_algorithm` is set explicitly
and doesn't match the key type (`intersight/signing.py:255-263`). Leaving
`signing_algorithm=None` (as the README example effectively lets happen
downstream) avoids this failure mode entirely — one more reason not to
hardcode it.

## 3. THE DECISIVE QUESTION — string vs file path, and passphrase

**Yes.** `HttpSigningConfiguration.__init__` accepts
`private_key_string` as a plain string parameter, independent of
`private_key_path`:

```python
def __init__(self, key_id, signing_scheme, private_key_path=None,
             private_key_string=None,
             private_key_passphrase=None,
             signed_headers=None,
             signing_algorithm=None,
             hash_algorithm=None,
             signature_max_validity=None):
```
(`intersight/signing.py:145-153`, full constructor signature.)

`_load_private_key` (`intersight/signing.py:229-241`) reads
`self.private_key_string` first if it is not `None`, and only falls back
to opening `self.private_key_path` from disk otherwise — so a caller
that only ever sets `private_key_string` never touches the filesystem. If
neither is usable it raises `"API Key either in file or as string is not
provided."` (`intersight/signing.py:241`), and the constructor itself
pre-checks this and raises `"Private key file or private key string not
provided."` if `private_key_path` doesn't exist on disk *and*
`private_key_string is None` (`intersight/signing.py:157-161`).

**This settles it for this project: the PEM can ride in an ordinary
environment variable** (`INVENTORY_INTERSIGHT_PASSWORD`, per this repo's
existing per-manager-type env convention), passed straight into
`private_key_string=`. No mounted secret volume is required by the SDK —
in direct contrast to `REDFISH_STANDALONE`'s per-host credential file
(`docs/adr/0016`), Intersight's key is one value, one env var, same shape
as every other collector in `tools/run_collector.py`.

Passphrase support is confirmed: `private_key_passphrase` is passed
straight through to `RSA.importKey(pem_data, self.private_key_passphrase)`
/ `ECC.import_key(pem_data, self.private_key_passphrase)` /
`PEM.decode(pem_data, self.private_key_passphrase)`
(`intersight/signing.py:245-260`) for RSA, EC, and PKCS8-wrapped keys
respectively. So the key does **not** need to be unencrypted — an
encrypted key plus its passphrase both fit as strings, e.g. two env vars
(`..._PASSWORD` for the PEM, plus a second var for the passphrase if
Intersight-issued keys are ever encrypted — see caveat below).

**Caveat, UNVERIFIED**: whether Intersight-generated download-once API
keys are ever passphrase-protected by default. The Cisco DevNet
authentication page states "This is your only opportunity to view, copy
and download the private key" but the fetched excerpt didn't describe
passphrase protection either way
([developer.cisco.com/docs/intersight/authentication/](https://developer.cisco.com/docs/intersight/authentication/)).
Treat the passphrase field as optional/likely-unused for a freshly
generated key; confirm by generating one against the target account.

## 4. What the API key actually is

Strictly **(API Key ID string, PEM private key)** — never a
username/password pair for the REST API. `HttpSigningConfiguration`
takes `key_id` (a string identifier) and a private key; there is no
username/password field anywhere in `signing.py` or in
`Configuration`'s `auth_settings()` construction of the `http_signature`
scheme (`intersight/configuration.py:452-457`, `'value': None  #
Signature headers are calculated for every HTTP request`).
`Configuration` does separately support `username`/`password` and
`access_token` (OAuth2 bearer) fields (`intersight/configuration.py`
constructor), but those are generic OpenAPI-generator scaffolding for
*other* auth schemes the generated client could in principle use, not
something Intersight's API accepts — the only `auth_settings()` entries
the shipped code ever populates for signing are `cookieAuth`,
`http_signature`, and `oAuth2` (bearer token), and the SDK's own
authentication example uses only `http_signature`
(`METADATA:104-160`).

**Key ID format**: three 24-character hex Mongo-style Moid segments
joined by `/`, e.g.
`61970b917564612d333d4d41/61970b917564612d333d4d46/626f24e57564612d335c906d`
— account Moid / API-key-owner Moid / API-key Moid
(observed in a search-indexed Cisco DevNet doc excerpt and consistent
with Intersight's Moid format elsewhere in its docs, e.g.
`5ddf1d456972652d30bc0a10` for an organization
([community.cisco.com "Getting started with the Intersight API browser"](https://community.cisco.com/t5/data-center-and-cloud-blogs/getting-started-with-the-intersight-api-browser/ba-p/4820802))).
**UNVERIFIED against a primary Cisco doc page directly** — WebFetch could
only retrieve a search-engine-indexed excerpt, not the live
`developer.cisco.com` page content for this specific field. Confirm by
generating a key in the target Intersight account and reading the Key ID
Intersight displays.

**On-prem Virtual Appliance**: not confirmed either way from these
sources. The task brief's premise — that the on-prem appliance might
offer a separate username/password REST path — is `UNVERIFIED`; nothing
fetched here describes Intersight Virtual Appliance auth as differing
from the SaaS API's key-based HTTP-signature scheme. If Intersight
Connected Virtual Appliance / Private Virtual Appliance auth needs to be
confirmed, that requires fetching Cisco's Virtual Appliance install/admin
guide specifically — not yet done here.

## 5. Clock skew

**Exact tolerance in seconds: UNVERIFIED.** No primary source fetched
states a number. What is confirmed: the `(created)` value is a
whole-second Unix timestamp with **no subsecond precision**
(`intersight/signing.py:281-284`, "Subsecond precision is not
supported"), and independently, a Cisco Community thread describes the
real-world failure mode as "the signature creation date in the
'Authorization' header is in the future," attributing it to client/server
clock desync
([community.cisco.com](https://community.cisco.com/t5/cisco-intersight/unauthorized-error-when-connecting-to-intersight-applicance/td-p/4110365)).
The SDK itself never surfaces a distinct "clock skew" exception type —
a skewed signature fails the same way any other invalid-signature
request fails: `rest.py:218-219` raises `UnauthorizedException` for any
HTTP 401 (`intersight/rest.py:217-219`), and neither `rest.py` nor
`exceptions.py` (not opened, but `UnauthorizedException` is a thin
`ApiException` subclass per the import at `intersight/rest.py:23`)
distinguishes *why* the server returned 401. **The distinguishing text,
if any, must come from the HTTP response body Intersight returns**,
which none of these sources quoted verbatim.

**What this means for `health_check()`**: don't expect the SDK to hand
you a named "clock skew" exception — catch `UnauthorizedException`
(HTTP 401) generically and inspect `http_resp.data`/`.reason` for
skew-indicating text, and independently cross-check the pod's own clock
against NTP as a first-class diagnostic before even calling Intersight,
since the SDK gives no better signal. Settling the exact tolerance and
response body requires either an authoritative Cisco doc (not found in
this pass) or an empirical test: sign a request with `(created)`
deliberately offset by increasing amounts against a real Intersight
account and observe where it starts failing.

## 6. Key rotation / expiry / revocation

**UNVERIFIED — no primary source fetched distinguishes these.** Two
Cisco Community URLs that looked likely to have exact wording
(`.../intersight-api-token-no-longer-working/...` and the unauthorized-error
thread) both returned **HTTP 403 Forbidden to WebFetch** — they require
an authenticated community login and could not be read in this pass. Web
search summaries of the community threads only reconfirmed the
future-clock 401 case (§5), not expired/revoked/wrong-key-id/malformed-PEM
specifically.

What the SDK code does confirm as *distinguishable without any network
call*, at construction time, purely from the PEM text supplied — i.e.
these four cases will never reach the network as generic 401s, they fail
locally with different messages:
- **Malformed PEM** (no `-----BEGIN ...-----` line at all): `_load_private_key`
  raises `ValueError("Not a valid PEM pre boundary")`
  (`intersight/signing.py:243-244`).
- **PEM header present but unrecognized** (not RSA/EC/PKCS8/encrypted
  PKCS8): raises `Exception("Unsupported key: {0}".format(pem_header))`
  (`intersight/signing.py:262-263`).
- **PKCS8-wrapped key with a non-EC OID**: raises `Exception("Unsupported
  key: {0}. OID: {1}")` (`intersight/signing.py:257-259`) — the SDK's
  PKCS8 branch only actually supports EC keys wrapped that way, despite
  accepting the generic `PRIVATE KEY`/`ENCRYPTED PRIVATE KEY` headers.
- **Empty/missing key material** (neither string nor path resolves):
  raises `Exception("API Key either in file or as string is not
  provided.")` (`intersight/signing.py:241`).

What's genuinely server-side and needs live testing against a real
account to characterize: **expired key** vs **revoked key** vs **wrong
key ID (valid format, unknown to Intersight)** vs **key ID that doesn't
match the signing key**. All four are structurally valid requests the
SDK will happily build and sign — the SDK cannot tell locally that a key
is expired/revoked/unknown, since it never queries key status, it only
ever signs. Expect Intersight to return HTTP 401
(`UnauthorizedException`, same code path as §5) for all four, with the
differentiation — if any exists — only in the response body text.
**Action item, not yet done**: generate a test API key, then deliberately
delete it / let it expire / use a syntactically-valid-but-wrong key ID
against a real Intersight tenant, and capture the three response bodies
verbatim. That is the only way to get four distinguishable
`health_check()` messages; nothing in the docs fetched here promises
they differ at all.

## 7. Least privilege

Cisco DevNet's role-catalog pages (indexed via search, not fetched live —
see caveat) name several system-defined roles, including:
- **Account Administrator** — full access, described as able to "perform
  all administrative and management tasks" — explicitly the wrong choice
  for a collection-only account.
- **Read-Only** — named in Terraform-provider/DevNet search results as a
  system-defined role a user (and by extension an API key scoped to that
  user) can hold
  ([search result summary](https://developer.cisco.com/docs/intersight/iam-read-a-iam-usersetting-resource/) — page not independently fetched in full).
- **Device Administrator** — "claim and unclaim a device... generate API
  keys" — a device/claim-management role, not inventory-read.
- **Audit Log Viewer**, **Catalog Administrator** — unrelated to compute
  inventory.

**Best-practice guidance found (secondary, not Cisco-authored)**: a
practitioner blog on Intersight RBAC argues system-defined roles are
generally too coarse and recommends **user-defined roles** scoped to
specific privileges instead of the built-in ones
([rednectar.net, "Your first big mistake with Intersight. Not using RBAC"](https://rednectar.net/2023/09/05/your-first-big-mistake-with-intersight-not-using-rbac/)).
That source did not name which built-in privilege maps to read-only
`compute.*` access, only that a custom role scoped to a resource group
is the recommended pattern in general.

**Recommendation, not fully verified**: the system-defined **Read-Only**
role is the closest documented fit for a collection account and is
almost certainly sufficient to read `compute.PhysicalSummary`,
`compute.Blade`, `compute.RackUnit` etc. (the resources a UCS-style
collector needs), but this project's own convention (research every
choice fresh, cite primary sources) is not fully met here — **confirm
directly in the target Intersight account's IAM → Roles page** which
privileges the "Read-Only" role actually grants before wiring it into a
provisioning runbook, since the fetched pages describe *what a role
does* qualitatively but never enumerated its underlying privilege list.
If finer scoping matters (e.g. denying access to non-compute resources
this collector never reads), build a user-defined role restricted to a
read privilege on the `compute` resource group instead, per the
rednectar.net recommendation above.

## 8. Never-log list

Confirmed by reading `configuration.py` and `api_client.py` directly, not
assumed:

- **Never log the private key** — never printed or logged anywhere in
  `signing.py`; it's held only as `self.private_key` (a `pycryptodome`
  key object, not the raw PEM after `_load_private_key` returns).
- **Never log the `Authorization` header value or the signature** — the
  SDK's own request/response debug logging in `rest.py` only logs the
  **response body** at debug level (`logger.debug("response body: %s",
  r.data)`, `intersight/rest.py:215`) — it does not log outbound request
  headers via its own logger calls.

**But there is a real leak path if debug mode is ever enabled**:
`Configuration.debug`'s setter, when set `True`, does two things
(`intersight/configuration.py:361-374`):
1. Sets the SDK's own loggers (`"intersight"`, `"urllib3"`) to
   `logging.DEBUG`.
2. Sets `http_client.HTTPConnection.debuglevel = 1` — this is CPython's
   stdlib `http.client` wire-level debug flag, which makes `http.client`
   **print the raw HTTP request, including all headers — the
   `Authorization` header carrying the signature included — directly to
   stdout** via its own `print()` calls, entirely outside the SDK's
   logger and outside anyone's log-level filtering.

**Conclusion for this project: never set `configuration.debug = True`
in the collector**, and don't route the collector's own log level in a
way that could flip it on inadvertently — it is a code path this SDK
version genuinely has, confirmed by reading it, not a hypothetical. A
production collector should leave `Configuration.debug` at its default
(`False`, `intersight/configuration.py:217`) unconditionally, with no
environment-variable or flag path that could set it `True` outside a
deliberate, human-run local debugging session.

## Summary of open items for whoever builds this collector

- Confirm the currently-issued key version (v2 RSA vs v3 EC) by
  generating one key against the real target tenant (§2).
- Confirm the Key ID's exact format against a real generated key (§4).
- Confirm exact clock-skew tolerance and the 401 body text for a skewed
  clock, by deliberately offsetting `(created)` against a real tenant
  (§5) — the SDK gives no better signal than a generic 401.
- Confirm the response bodies for expired/revoked/wrong-key-id/
  mismatched-key-id against a real tenant (§6) — all four are
  indistinguishable from the SDK's own exception type
  (`UnauthorizedException`) and require live testing; four of *these*
  four cases fail identically as a generic 401 while four *other* cases
  (malformed PEM, empty key material, bad PKCS8 OID, unrecognized PEM
  header) fail locally with distinct messages before any network call.
- Confirm the "Read-Only" system role's actual privilege list in the
  target tenant's IAM → Roles page before provisioning the collection
  account (§7).
