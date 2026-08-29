# Cisco Intersight collector testability — research notes

**2026-08-29.** Question: can an Intersight collector be validated without
real hardware, the way the UCS Manager collector was validated against
UCSPE, and specifically — can it be tested or even *deployed* at all given
this platform's air-gapped constraint? **Short answer: no reachable option
today reproduces the UCSPE result** (a real manager binary answering real
API calls against simulated hardware, runnable locally). The DevNet
Intersight sandbox that used to offer this is currently offline for a
platform-wide rebuild with no fixed return date before ~Q1 2027. The
air-gap question resolves cleanly, though: **an air-gapped site can only
ever reach Intersight via the Private Virtual Appliance (PVA)**, an
on-premises OVA that Cisco's own docs describe as serving the same API —
but nothing in this research reached the PVA's own API reference to
confirm parity first-hand, so that specific claim is `UNVERIFIED` against
a primary source and should be reconfirmed against the appliance's own
`/apidocs` before it's trusted for a build decision.

## 1. Cisco DevNet Intersight Sandbox

**Currently offline, no ETA before early 2027.** Cisco's own developer blog
states: "Beginning August 1st, the current Sandbox platform will
temporarily transition offline" with the goal to have it "back online
early 2027," later firmed up elsewhere in the same rebuild communication
as a Q1 CY27 target
(https://blogs.cisco.com/developer/devnet-sandbox-rebuild-future-developer-experiences,
published 2026-06-01, updated 2026-08-05 confirming the transition was
underway). Today's date in this session is 2026-08-29, so the sandbox
catalog is down *right now* for this project.

Before that shutdown, an Intersight sandbox did exist in the DevNet
catalog: "DevNet provides an Intersight Sandbox that allows users to
create an Intersight account and claim emulated UCS Hardware... a great
place to try out the Intersight API and programming tools without using
your own physical Hardware" (via Cisco Community post "Learn How to
Automate with Intersight on DevNet and at Cisco Live!",
https://community.cisco.com/t5/data-center-and-cloud-blogs/learn-how-to-automate-with-intersight-on-devnet-and-at-cisco/ba-p/3663534
— page itself returned HTTP 403 to direct fetch, so this is via search
snippet only, not a full primary read; treat the exact mechanics as
**UNVERIFIED**). DevNet's general sandbox catalog documents two access
modes, Always-On (shared, no reservation, restricted admin access) and
Reservation (VPN, full admin) (https://developer.cisco.com/docs/sandbox/,
https://developer.cisco.com/docs/sandbox/first-reservation-guide/) — which
mode the Intersight sandbox used is **UNVERIFIED**, since the catalog
itself can't be browsed right now to check.

**What this means for the build decision:** even if the sandbox is judged
worth waiting for, it will not exist to validate against for roughly the
next five months at minimum, and Cisco has given no committed relaunch
date, only an aim. Treat "wait for DevNet sandbox" as not currently a
plan, not as a near-term option.

## 2. Free-tier Intersight SaaS (no claimed hardware)

Anyone can create an intersight.com account and generate an API key with
no special privilege: "Any user (Read-Only or Account Administrator) can
generate API keys" from `https://intersight.com/an/settings/api-keys/`
(https://developer.cisco.com/docs/intersight/authentication/). That page
does not say whether a license tier or claimed device is required to
*generate* a key — that gap is **UNVERIFIED** from this source alone.

A separate community source (search snippet, not a direct primary fetch —
https://community.cisco.com/t5/data-center-and-cloud-knowledge-base/intersight-api-overview-including-powershell-and-python-demos/ta-p/3651994)
states plainly: "Any Intersight API can only retrieve info for claimed
devices in that account" and that API access requires "credentials
[that] must belong to an Essentials or higher licensed Intersight
portal." Base/Essentials-tier accounts are Cisco's free entry tier for
Intersight SaaS, so this is consistent with "mint a key at no cost," but
the exact tier name and whether it is truly $0 was not independently
confirmed against Cisco's own licensing page in this pass —
**UNVERIFIED**, would be settled by Cisco's Intersight licensing/pricing
page or by actually creating an account.

**What a key with zero claimed devices would prove, and what it would
not:** a `compute.PhysicalSummary.List` GET against an empty account
would almost certainly return HTTP 200 with an empty `Results` array —
that's how every REST collection endpoint here behaves per the OpenAPI
contract's shape (no special-cased "no devices" error is documented
anywhere found in this pass) — but this is inference from API convention,
not a fetched example response, so mark the *empty-200* claim
**UNVERIFIED** too; it would take one free account to confirm outright.
If true, it would prove: HTTP-Signature request signing works end to end,
pagination/`$top`/`$skip` parameters are accepted, and error-vs-empty
handling on the collector's happy path. It would prove **nothing** about
real field population, units, nullability, or parent-relationship
correctness for `compute.PhysicalSummary`, `compute.Blade`,
`compute.RackUnit`, etc. — exactly the class of defect UCSPE caught for
UCS Manager (empty-in-practice fields, wrong units, wrong DN structure).
No hardware means no real inventory payload, so this option validates
plumbing, not data-shape.

## 3. Intersight Virtual Appliance (both flavours)

Cisco's own Getting Started Guide overview page describes two on-premises
appliance flavours, both delivered as an OVA:

- **Connected Virtual Appliance** — on-prem OVA that still talks out to
  intersight.com for the SaaS control plane; used where local endpoint
  presence is wanted but a live internet path exists.
- **Private Virtual Appliance (PVA)** — "intended for environments where
  you operate data centers in a disconnected (air gap) mode... delivers
  the management features of Intersight while ensuring no system details
  leave your premises" (search-snippet paraphrase sourced to
  https://www.cisco.com/c/en/us/td/docs/unified_computing/Intersight/b_Cisco_Intersight_Appliance_Getting_Started_Guide/m_appliance_overview.html
  — this specific Cisco TD docs page returned HTTP 403 to direct
  WebFetch in this session, on every URL variant tried, so this is
  **from search-engine snippets of that page, not a direct primary
  read**; re-fetch it — ideally with an authenticated/logged-in session,
  since Cisco's TD docs site increasingly gates crawler UAs — before
  treating any exact wording here as load-bearing).

**Sizing** (from search snippets citing the same Getting Started Guide's
Installation section, same 403-to-direct-fetch caveat applies): Tiny (8
vCPU / 16 GB RAM, Intersight Assist only, not the full appliance), Small
(16 vCPU / 32 GB RAM), Medium (24 vCPU / 64 GB RAM / 2 TB storage), Large
(48 vCPU / 96 GB RAM / 2 TB storage). Even the smallest full-appliance
tier is a real VM commitment, not a laptop-friendly emulator — nothing
like UCSPE's lightweight footprint.

**Licensing:** commercial SKUs exist for both flavours at both Advantage
and Premier tiers (`DC-MGT-IS-PVAPP-AD`/`DC-MGT-PVAPP-PR` for Private
Virtual Appliance, `DC-MGT-IS-SAAS-AD` for Connected), visible on
reseller listings (CDW, Insight, Hummingbird Networks) rather than
Cisco's own price list directly — so the *tier name* is corroborated by
three independent resellers, but "is there a free/eval SKU" is
**UNVERIFIED**; nothing found in this pass suggests a no-cost PVA
license exists. This alone is a material testability gap versus UCSPE,
which is a free download requiring only a Cisco.com login.

**Can it be run locally as a test target?** Architecturally yes — it's an
OVA deployable to any vSphere/similar hypervisor — but between the
minimum 16–48 vCPU / 32–96 GB sizing and the licensing requirement, this
is a real infrastructure and cost commitment, not a "spin it up on a
laptop to check a collector" option the way UCSPE was.

## 4. The air-gap question — most important finding

**An air-gapped site can reach Intersight at all only through the Private
Virtual Appliance — never through intersight.com, and never through the
Connected Virtual Appliance**, because the Connected flavour still phones
home to the public SaaS control plane by design (search-snippet
description via the same Getting Started Guide overview page cited in
§3). `intersight.com` is public internet SaaS; an air-gapped deployment
by definition has no route to it. That much is a direct logical
consequence of the deployment's own constraint, not something requiring
a citation to prove.

**Does the PVA serve the identical API surface and the same MOs?** Cisco's
Private Virtual Appliance "what's new" help page
(https://intersight.com/help/appliance/whats_new/private_appliance/2020)
was reachable but rendered no usable body content to WebFetch (it's a
JS-driven SPA help center, same as the main `intersight.com/apidocs/`
pages) — **this specific claim was not confirmed against a primary
source in this pass.** The Getting Started Guide overview snippet says
the PVA delivers "the management features of Intersight," which implies
API parity by design intent, but that is marketing-level language, not a
verified statement that `compute.PhysicalSummary`, HTTP-Signature auth,
and the OpenAPI contract are byte-for-byte identical on a PVA's own FQDN.
**Mark API/MO parity claim UNVERIFIED** — the thing that would settle it
is fetching the PVA's own `/apidocs` from a running instance (or its
install guide's API reference chapter, which also 403'd to direct fetch
in this pass and needs a retry with a real browser session or an
authenticated Cisco.com login).

**Verdict:** *An air-gapped deployment of this platform can use an
Intersight collector, but only by first standing up a Private Virtual
Appliance on-premises and pointing the collector at that appliance's own
FQDN instead of `intersight.com` — never at the public SaaS endpoint.*
This is a materially bigger prerequisite than every other collector this
platform has: UCS Central/Manager, Redfish-standalone, and (per this
repo's stated plan) OneView/OpenManage are all reached directly at
customer-owned management-plane IPs already inside the air gap. An
Intersight collector instead requires the *customer* to first deploy and
license a multi-tens-of-GB Cisco appliance before this platform can talk
to it at all — that's a deployment dependency this repo doesn't currently
carry for any other vendor.

## 5. Recorded fixtures from the OpenAPI spec alone

Checked directly:
`https://developer.cisco.com/docs/intersight/compute-physicalsummary-list/`
(the DevNet-hosted API reference page for exactly the endpoint this
collector would use) renders schema/type metadata, security-scheme
definitions and privilege requirements — **no example JSON response body
with populated sample values was present on the page.** This is a direct
observation of the actual page content, not a secondhand snippet.

The `intersight-python` SDK
(https://github.com/CiscoDevNet/intersight-python) is stated (via search
result, not a direct repo read in this pass) to be code-generated
straight from the same OpenAPI 3.x spec — i.e., it carries the same
type/schema information already checked above, not independently curated
example payloads. No SDK package is installed in this repo's environment
to inspect docstrings directly (`find / -iname '*intersight*' -path
'*/site-packages/*'` returned nothing). CiscoDevNet does publish worked
examples (`intersight-python-utils`, `intersight_python_examples`,
`intersight-jupyter-notebooks` — including a notebook specifically titled
"Getting Physical Compute Inventory from Intersight... with the Python
SDK") but these are runnable *code* against a *real* account, not static
fixture data — they produce nothing without a live target to run them
against, which circles back to §§2–4.

**Conclusion, stated plainly as the task asked: no, hand-written fixtures
built only from the OpenAPI schema (types, not values) cannot catch the
class of bug UCSPE caught for UCS Manager.** That class of bug was
specifically "the type is right but the value is empty/wrong-unit/wrong-
parent in practice" — by construction, a schema only constrains type and
shape, never real-world population behavior. Confirming this required
checking that the actual reference page carries no example values, which
it doesn't.

## 6. Recommendation, ranked

1. **Free-tier Intersight SaaS account with zero claimed devices** (§2) —
   best available option *today*. Proves request signing, pagination,
   and empty-result handling for near-zero cost and no infrastructure.
   Does not prove anything about real field population — treat any
   fixture built this way as "the plumbing compiles," not "the data
   shape is right."
2. **Wait for DevNet Intersight sandbox to return** (§1) — closest
   equivalent to what UCSPE gave the UCS Manager build (emulated
   hardware, real manager binary, real data), but is not an option for
   roughly the next five months minimum and carries no committed date.
   Re-check `https://developer.cisco.com/site/sandbox/` before assuming
   this timeline still holds.
3. **Stand up a Private/Connected Virtual Appliance** (§3) — would give
   the most realistic non-hardware validation (a real appliance, real
   API, real field behavior against whatever compute is actually
   claimed to it), but requires a real license and 16–48 vCPU / 32–96 GB
   of infrastructure, which is a materially bigger ask than every other
   collector this platform has built so far. Only worth it once real
   Intersight-managed hardware (or at minimum a licensed appliance) is
   available to claim into it — an appliance with nothing claimed proves
   the same thing as option 1, at far higher cost.
4. **Hand-written fixtures from the OpenAPI schema alone** (§5) — worth
   doing for basic shape/type checks in unit tests regardless, but
   explicitly cannot replace either of the above for catching real-data
   defects, and should not be presented as equivalent to a UCSPE-style
   validation pass.

**Bottom line for the build-order decision this note was requested to
inform:** Intersight is reachable and testable-to-a-point today at
near-zero cost via option 1, but nothing available right now reproduces
what UCSPE gave UCS Manager, and the air-gap constraint means any real
production Intersight collector for this platform depends on a
customer-deployed, separately-licensed Private Virtual Appliance — a
dependency none of this platform's other collectors carry. That's a real
factor against building Intersight next purely on "easiest to actually
test," independent of whichever vendor turns out to have better testable
access.

## Open questions / UNVERIFIED

- **PVA API/MO parity with SaaS** (§4) — the single most important open
  item, since it decides whether "test against SaaS" work transfers to
  the air-gapped target at all. Settle by fetching a running PVA's own
  `/apidocs` (or the install guide's API reference chapter — both
  `cisco.com/c/en/us/td/...` URLs 403'd to a plain WebFetch in this
  session and need retrying with an authenticated session or a browser
  rather than a bare crawler UA).
- **Whether generating an API key on a free/Base-tier SaaS account
  actually requires zero cost and zero claimed hardware** (§2) — the
  "Essentials or higher licensed portal" requirement was seen only in a
  search snippet of a community post, not read directly (the URL 403'd).
  Settle by creating an account.
- **Whether an empty-account `compute.PhysicalSummary.List` truly returns
  HTTP 200 with an empty array** vs. some other error shape (§2) —
  inferred from REST convention, not observed. Settle with the same free
  account.
- **Exact current state/contents of the DevNet Intersight sandbox
  catalog entry** (§1) — description here is from a search snippet of a
  Cisco Community post that itself 403'd to direct fetch, and the whole
  catalog is offline to browse right now. Settle by re-checking
  `https://developer.cisco.com/site/sandbox/` once the rebuild ships.
- **Whether a free/eval Private Virtual Appliance license exists** (§3)
  — every SKU found was a paid Advantage/Premier commercial license via
  resellers, none from Cisco's own price list directly. Settle via a
  Cisco account team or Cisco's own commerce/licensing page.
