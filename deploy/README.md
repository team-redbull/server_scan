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

## Collectors (CronJobs)

Real vendor collectors run as Kubernetes `CronJob`s, one per manager
*type*, invoking `tools/run_collector.py --manager-type <TYPE>` in the
same image as the API (`Containerfile` copies `tools/` alongside `app/`
specifically so no second image is needed). See the repo root
`README.md`'s "How data actually gets in" section and
`docs/adr/0009-ucs-manager-collector.md` for the full design.

- `openshift/ucs-manager-collector-cronjob.yaml` / `helm/.../templates/
  ucs-manager-collector-cronjob.yaml` — the only vendor implemented so
  far. Each UCS Manager domain needs its own credentials `Secret`
  (`username`/`password` keys), projected into the pod at
  `/etc/inventory/credentials/{credential_ref}/` — the Helm chart's
  `collectors.ucsManager.managers` list generates that projection from a
  plain list; the raw OpenShift YAML has one hand-written example and
  needs a new `sources` entry per manager if you're not using Helm.
- `OPENMANAGE`/`INTERSIGHT`/`ONEVIEW` have no collector or CronJob yet —
  add both together when that vendor's `ServerInventoryProvider` lands.

## Current state

The backend API has full manifests (Deployment, Service, Route/Ingress,
ConfigMap) in both `openshift/` and the Helm chart. **The frontend does
not yet have equivalent Kubernetes manifests** — its `Containerfile`
(UBI9 + nginx, static Vite build) has existed since slice 1, but nothing
in `deploy/` deploys it; this is a real, open gap, not a placeholder
waiting on the build to exist. Also still open: no CI job builds or
pushes a container image (CI only lints/tests), and this platform has no
CD/GitOps wiring of its own — both are deployment-pipeline concerns for
whatever GitOps tooling consumes these manifests, tracked as pending work
in `CLAUDE.md`.
