# ADR-0010: Every push to main publishes both images to GHCR, versioned by Conventional Commits

## Status

Accepted

## Context

CI (`.github/workflows/ci.yml`) only ever lint/tested — nothing built or
published a container image, and nothing decided what version a given
build even was. The platform has two images (the API/collector image
from the root `Containerfile`, and the frontend from
`frontend/Containerfile`) that needed a real publishing story before
either could be deployed anywhere.

Two decisions were needed: where to publish, and how to decide a
version number automatically on every push rather than requiring a
human to hand-pick one each time.

## Decision

### Registry: GitHub Container Registry (ghcr.io)

Already the natural choice given the repo lives on GitHub: no separate
registry account/credentials to provision, and `GITHUB_TOKEN` (the
token every workflow run already gets, scoped to just this repo) is
sufficient to push — no new secret to create or rotate. `docker/
login-action@v4` with `username: ${{ github.actor }}` /
`password: ${{ secrets.GITHUB_TOKEN }}` is the standard pattern for
this; the job just needs `permissions: packages: write` declared.

### Versioning: Conventional Commits, automatic, patch-by-default

`mathieudutour/github-tag-action@v6` scans every commit since the last
tag and picks the bump: a `BREAKING CHANGE:` footer or `!` after the
type (`feat!:`) → major, `feat:` → minor, anything else (`fix:`,
unprefixed, ...) → patch (`default_bump: patch`). It pushes the new git
tag itself (`permissions: contents: write`) and outputs `new_tag` (e.g.
`v1.4.2`) for the image-tagging steps to consume.

Chosen over hand-picking a version per push (defeats the point of
automating this) and over "always bump patch, tag major/minor by hand"
(considered — genuinely simpler, zero commit-message discipline
required — but Conventional Commits is standard enough, and expressive
enough to capture an intentional breaking change automatically, that it
was worth the one real cost below).

**The real cost, stated plainly**: this repository's commit history
before this ADR does not follow Conventional Commits, and adopting this
means future commit messages should — starting a message with `feat:`,
`fix:`, `feat!:`/a `BREAKING CHANGE:` footer for anything that actually
warrants a minor or major bump. Until that habit is established, every
push still bumps patch safely (the `default_bump: patch` fallback) —
this doesn't produce wrong version numbers, it just means every release
looks like a patch release until commit messages start signaling
otherwise on purpose.

### Tag scheme

Both images get the same set, generated from one version string by
`docker/metadata-action@v6` (the tool built for exactly this — avoids
hand-writing the semver-component-splitting logic):

- The full version (`v1.4.2`)
- Rolling `{major}.{minor}` (`v1.4`) and `{major}` (`v1`) lines
- `latest`
- `sha-<short-sha>` — exact traceability back to source between
  releases, independent of the semver line

### One job, both images, gated on the rest of CI

`publish` `needs: [lint, test, frontend, e2e]` and only runs
`if: github.event_name == 'push' && github.ref == 'refs/heads/main'` —
it never runs for a pull request, and never publishes anything that
hasn't passed the full suite first. Both images build for
`linux/amd64` only, matching the existing `Containerfile`'s own
x86_64-specific Python download (python-build-standalone) — multi-
platform (`docker/setup-qemu-action` + a platform matrix) is a real
option later if arm64 is ever actually needed, not before.

## Consequences

- Commit messages from here on should follow Conventional Commits when
  a change is more than a patch — this is now a real, functional signal
  (it changes the published version), not just a style nicety. Past
  commits don't need to be rewritten; the tag action only looks forward
  from the last tag.
- The very first run of this job has no prior tag to compare against —
  `mathieudutour/github-tag-action` starts from `0.0.0` (or `0.1.0`
  depending on its own defaulting — verify the actual first tag it
  produces on the first real run and adjust expectations, don't assume
  in advance).
- Both `ghcr.io/team-redbull/server_scan-api` and
  `ghcr.io/team-redbull/server_scan-frontend` inherit the repository's
  visibility (private repo → private package) by default; no separate
  visibility configuration was added here.
- Build provenance attestation (`actions/attest-build-provenance`) and
  multi-arch builds are reasonable future hardening steps, deliberately
  not included here — scope was "publish, versioned" per the request
  that prompted this ADR, not a broader supply-chain-security pass.
