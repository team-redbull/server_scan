# Deployment

Two equivalent manifest sets for the same target — plain OpenShift YAML
under `openshift/`, and a Helm chart wrapping the same resources under
`helm/`. Use whichever fits your GitOps tooling; they are not meant to be
applied together.

## Scope

These manifests deploy the **API and frontend only**. They deliberately do
**not** stand up MongoDB or Redis in the cluster:

- MongoDB is the system of record and, in a real air-gapped production
  estate, is expected to already exist as an operated service (with its
  own backup, replication, and upgrade story) that this platform is
  pointed at — not a database this application owns the lifecycle of.
- Redis is an ephemeral cache; a small in-cluster instance is reasonable,
  but is equally fine to omit — every read path degrades to MongoDB on a
  cache miss or a Redis outage (`app.infrastructure.redis`), so Redis is
  never a hard dependency for this platform to run.

Both connection strings arrive via a `Secret` (`server-inventory-db`, keys
`mongo-uri` / `redis-uri`) that this manifest set consumes but does not
create — provisioning it is a platform/GitOps concern, consistent with the
"no credentials in source, credentials via secret refs" requirement.

## Container security

The image (`Containerfile`, repo root) runs as a fixed non-root UID as a
sane local default, but nothing in these manifests pins `runAsUser` —
OpenShift's `restricted-v2` SCC assigns a UID from the namespace's
allocated range at admission time, and the image writes nothing to disk at
runtime (all logging is to stdout), so it runs correctly under whatever UID
the SCC assigns without an `anyuid` grant.

## Current state

Phase 1 slice 0: manifests deploy the API skeleton (health probes, metrics)
only — no domain routes exist yet. The frontend manifests are placeholders
until the Vite build and its serving image exist (slice 1).
