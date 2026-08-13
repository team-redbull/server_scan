# ADR-0005: `policy_key` families for health policy overrides

## Status

Accepted

## Context

The platform spec requires three things simultaneously from the health
policy engine:

1. Many independent policies must fire simultaneously (a fabric-down
   policy and a failed-drive policy on the same server both produce their
   own evaluation).
2. A site-scoped policy must be able to *replace* a global default (e.g.
   "2 fabric paths down = CRITICAL" globally, but "= WARNING" for a lab
   site) — not just add a second, conflicting alert alongside it.
3. A scope must be able to disable an inherited default outright.

A plain "highest priority wins" rule collapses requirement 1 — only one
policy would ever evaluate per server. "Every matching policy fires"
satisfies requirement 1 but makes requirement 2 impossible: the global
CRITICAL policy would still fire alongside the site's WARNING one, and the
server's overall severity would still be CRITICAL regardless of the
override.

## Decision

Every `HealthPolicy` carries a `policy_key` (`app.domain.models.
health_policy`), defaulting to its own `id` when not explicitly shared.
Policies sharing a `policy_key` form a **family**; within a family,
resolution (`app.domain.services.health.evaluate.resolve_families`) picks
exactly one winner — highest scope specificity, then priority — and only
that winner is evaluated. Policies with *different* keys are fully
independent families and all evaluate. A disabled family winner means the
family contributes nothing (this is how a scope disables an inherited
default: author a same-key, higher-precedence, disabled policy). A winner
in `SUPPRESS` mode likewise contributes nothing, but is recorded as an
explicit, auditable suppression rather than looking identical to "nobody
authored a policy for this".

This resolves all three requirements: different-key policies (fabric-down,
failed-drive) evaluate independently (1); a site override shares the
global default's `policy_key` and wins on specificity, replacing it
entirely for that scope (2); a disabled or `SUPPRESS`-mode same-key policy
disables the family for its scope (3).

## Why not the alternatives

| Alternative | Why rejected |
|---|---|
| Field-level merge/patch of a base policy | Merge semantics for a nested condition tree are ambiguous — does a site's `condition` replace or intersect the global one? Unexplainable in a UI ("this policy is a merge of policy A and policy B" is not a sentence an operator can act on). |
| A `disabled_policy_ids` list per scope | Expresses *disable* but not *override* — there's no way to say "replace this policy's severity for my site" without a second mechanism anyway, and nothing links the disabling entry to what it disabled. |
| Severity-max across all matching policies, no shadowing | Directly contradicts requirement 2: the global CRITICAL would still win over the site's WARNING regardless of the override's intent. |
| Ordered rule list, first-match-wins across *all* policies (the classification-engine's own algorithm) | Kills requirement 1 — only one evaluation would ever be produced per server, and a fabric policy could accidentally suppress an unrelated storage policy just by sorting first. |

`policy_key` is the smallest addition that expresses "these policies are
about the same thing" — which is exactly, and only, the information
requirement 2 needs and no other mechanism supplies without also breaking
requirement 1.

## Consequences

- **A typo in `policy_key` silently creates a new family instead of
  overriding one.** `connectivity.fabric_paths_down_warning` vs
  `..._paths_down_warning` (missing an `s`) would produce two independent
  alerts where the operator expected one replaced. There is no
  registered-key guardrail in Phase 1 — the three seeded default policies
  use deliberate, documented keys (`app.domain.services.health.
  health_policy_defaults`), but a future admin-authored override is only
  as safe as the operator typing the key correctly. A near-duplicate-key
  warning at write time (Levenshtein distance against existing keys) is a
  reasonable follow-up if this proves to be a real operational hazard.
- **Verified live, not just unit-tested**: creating a `GLOBAL_CUSTOM`
  policy sharing the seeded `connectivity.fabric_paths_down_warning` key
  with a higher priority and `severity: INFO` flipped a real seeded
  server's health from WARNING to INFO with no code change — see
  `docs/architecture.md`'s health-policy-engine section.
