# Deployment

One Helm chart under `helm/server-inventory`, and nothing else.

There used to be a parallel set of plain OpenShift YAML under
`openshift/`. It was removed rather than maintained: it held the same
five resources as the chart's templates, with nothing checking the two
agreed, and they had already drifted — a change to the collector's
credential handling had to be made twice and only landed fully in one.
`helm template` covers the "I want plain YAML" case on demand:

```bash
helm template server-inventory deploy/helm/server-inventory \
  -f my-values.yaml > manifests.yaml
```

and both ArgoCD and Flux render Helm natively, so a GitOps setup needs no
pre-rendered copy either.

## Scope

These manifests deploy the **API and collectors only**. They deliberately
do **not** stand up MongoDB or Redis in the cluster:

- MongoDB is the system of record and, in a real air-gapped production
  estate, is expected to already exist as an operated service (with its
  own backup, replication, and upgrade story) that this platform is
  pointed at — not a database this application owns the lifecycle of.
- Redis is an ephemeral cache; a small in-cluster instance is reasonable,
  but is equally fine to omit — every read path degrades to MongoDB on a
  cache miss or a Redis outage (`app.infrastructure.redis`), so Redis is
  never a hard dependency for this platform to run.

Both connection strings arrive via a `Secret` (`server-inventory-db`, keys
`mongo-uri` / `redis-uri`) that this chart consumes but does not create —
provisioning it is a platform/GitOps concern, consistent with the "no
credentials in source, credentials via secret refs" requirement.

## Container security

The image (`Containerfile`, repo root) runs as a fixed non-root UID as a
sane local default, but nothing here pins `runAsUser` — OpenShift's
`restricted-v2` SCC assigns a UID from the namespace's allocated range at
admission time, and the image writes nothing to disk at runtime (all
logging is to stdout), so it runs correctly under whatever UID the SCC
assigns without an `anyuid` grant.

## Collectors (CronJobs)

Real vendor collectors run as Kubernetes `CronJob`s, one per manager
*type*, invoking `tools/run_collector.py --manager-type <TYPE>` in the
same image as the API (`Containerfile` copies `tools/` alongside `app/`
specifically so no second image is needed). See the repo root
`README.md`'s "How data actually gets in" section and
`docs/adr/0009-ucs-manager-collector.md` for the full design.

A collector's entire connection config is one endpoint and one login per
manager type, set in `collectors.<vendor>` in `values.yaml`. There are no
`Manager` documents to create first and no credentials volume to mount.
Those values render into a single `Secret`
(`templates/collector-credentials-secret.yaml`) and reach the pod as
`INVENTORY_*` environment variables via `envFrom`.

**Do not commit real passwords to `values.yaml`.** Pass them at install
time (`--set collectors.ucsManager.password=...`), from a values file kept
out of git (`-f secrets.yaml`), or — for production — set
`collectors.existingSecret` to a Secret managed by Vault, External
Secrets or sealed-secrets, which makes the chart skip rendering its own.

`envFrom` a Secret rather than inline `env` values is deliberate:
`kubectl get cronjob -o yaml` and `kubectl describe pod` both print plain
`env` values to anyone who can read workloads in the namespace, while a
`secretRef` shows only the reference.

Only `UCS_MANAGER` has a collector so far. `OPENMANAGE`, `INTERSIGHT` and
`ONEVIEW` have configuration slots but no provider — add the CronJob
template together with that vendor's `ServerInventoryProvider`.

Note that Intersight's three fields mean something different: it signs
requests with an API key rather than logging in, so `username` is the API
Key ID and `password` the secret key.

## Current state

The backend API has full manifests (Deployment, Service, Route,
ConfigMap). **The frontend does not yet have equivalent Kubernetes
manifests** — its `Containerfile` (UBI9 + nginx, static Vite build) has
existed since slice 1, but nothing here deploys it; a real, open gap.

CI does now build and publish both images to GHCR on every push to main
(`docs/adr/0010-image-publishing-and-versioning.md`), but nothing
*deploys* them: there is no CD/GitOps wiring and no automatic manifest
update, tracked as pending work in `CLAUDE.md`.
