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

The chart deploys the API, the collectors and — since 0.2.0 — optionally
the frontend, MongoDB and Redis. Which of those it stands up is four
independent switches, so a deployment picks its own shape:

| Value | Default | What it adds |
|---|---|---|
| *(always)* | — | API Deployment/Service/Route/ConfigMap |
| `frontend.enabled` | `false` | The React SPA (Deployment/Service/Route) |
| `mongodb.enabled` | `false` | A bundled MongoDB (Bitnami subchart) |
| `redis.enabled` | `false` | A bundled Redis (Bitnami subchart) |
| `collectors.<vendor>.enabled` | `false` | That vendor's CronJob |

**Both databases stay off by default**, because the original reasoning
still holds for a real production estate:

- MongoDB is the system of record and, in a real air-gapped production
  estate, is expected to already exist as an operated service (with its
  own backup, replication, and upgrade story) that this platform is
  pointed at — not a database this application owns the lifecycle of.
- Redis is an ephemeral cache; a small in-cluster instance is reasonable,
  but is equally fine to omit — every read path degrades to MongoDB on a
  cache miss or a Redis outage (`app.infrastructure.redis`), so Redis is
  never a hard dependency for this platform to run.

What bundling exists for is the case that reasoning does not cover: a lab,
a demo or a small site with no operated MongoDB/Redis to point at. Turning
`mongodb.enabled` on means this chart owns the lifecycle of the platform's
only durable state, backups included — a deliberate trade, not a default.

Each database is resolved on its own, so all four combinations render:

```bash
# Neither — the default. Both URIs come from db.secretName.
helm template server-inventory deploy/helm/server-inventory

# Both, plus the UI and the fake-data collector: a self-contained demo.
helm install si deploy/helm/server-inventory \
  --set mongodb.enabled=true --set mongodb.auth.rootPassword=... \
  --set 'mongodb.auth.passwords[0]=...' \
  --set redis.enabled=true --set redis.auth.password=... \
  --set frontend.enabled=true --set route.host=scan.apps.example.com \
  --set collectors.fake.enabled=true --set backend.cursorSecret=...

# Redis only, against an operated MongoDB.
helm install si deploy/helm/server-inventory \
  --set redis.enabled=true --set redis.auth.password=...
```

For whichever database is **not** bundled, the connection string arrives
via a `Secret` (`db.secretName`, default `server-inventory-db`, keys
`mongo-uri` / `redis-uri`) that this chart consumes but does not create —
provisioning it is a platform/GitOps concern, consistent with the "no
credentials in source, credentials via secret refs" requirement. A
bundled one is rendered into the chart's own `<release>-bundled-db` Secret
instead, and the two names are deliberately different so bundling one
database never collides with a `server-inventory-db` an operator owns.

**Set the bundled passwords explicitly.** Left blank, the Bitnami subchart
generates one — and `helm template`, which is how Argo CD renders this
chart, has no `lookup`, so a generated password is re-minted on every sync
while the database keeps the first one.

**Do not commit them.** For anything beyond a throwaway install, set
`mongodb.auth.existingSecret` / `redis.auth.existingSecret` and let a
secrets operator own the Secret. That changes where the *connection string*
comes from too: the chart never sees the password, so it cannot compose a
URI around it, and it falls back to `db.secretName` for that database. One
Secret then carries both — the subchart's own key
(`mongodb-root-password` + `mongodb-passwords`, or whatever
`redis.auth.existingSecretPasswordKey` names) and the `mongo-uri` /
`redis-uri` the API reads:

```bash
oc create secret generic server-inventory-db \
  --from-literal=mongodb-root-password='...' \
  --from-literal=mongodb-passwords='...' \
  --from-literal=redis-password='...' \
  --from-literal=mongo-uri='mongodb://server_inventory:...@server-inventory-mongodb:27017/server_inventory?authSource=server_inventory' \
  --from-literal=redis-uri='redis://:...@server-inventory-redis-master:6379/0'
```

Air-gapped installs need the subcharts vendored: `helm dependency update
deploy/helm/server-inventory` on a connected machine, then commit the
resulting `charts/*.tgz`. Note that Bitnami's charts default their image
to `:latest`; `values.yaml` says how to pin one.

## Image tags

Both images default to the chart's `appVersion` — a real release like
`11.0.2`, published by CI as `X.Y.Z` with no leading `v`
(docker/metadata-action's `{{version}}` strips it, so the git tag `v11.0.0`
becomes the image tag `11.0.0`). Upgrading is a bump of `appVersion`, or of
`backend.image.tag` / `frontend.image.tag` to override one image.

**Do not point these at `latest`.** It reads as "always current" and is the
opposite: `pullPolicy: IfNotPresent` means a node that already holds a
`latest` layer never pulls it again, so the cluster keeps running the first
build it ever saw — through a pod delete, a rollout and every later release.
That is not hypothetical here; it is how this chart's first deployment
failed. A pinned tag also gives GitOps something to diff and roll back,
which a floating one cannot.

## The frontend

`frontend.enabled` deploys the SPA image CI already publishes. The SPA
calls the API same-origin — `frontend/src/api/client.ts` sets no base URL
and its nginx proxies nothing — so the two Services share one host and are
split by path: the frontend takes `/`, and the API gets one Route per
entry in `route.apiPaths` (`/api`, `/health`, `/metrics`, `/docs`,
`/openapi.json`). OpenShift routes by longest prefix, so nothing has to
know about anything else.

That makes `route.host` **mandatory** once the frontend is on: an
OpenShift-generated host is derived per Route, so two Services would land
on two hostnames and every API call from the SPA would 404. The chart
fails to render rather than deploying that.

## Container security

The image (`Containerfile`, repo root) runs as a fixed non-root UID as a
sane local default, but nothing here pins `runAsUser` — OpenShift's
`restricted-v2` SCC assigns a UID from the namespace's allocated range at
admission time, and the image writes nothing to disk at runtime (all
logging is to stdout), so it runs correctly under whatever UID the SCC
assigns without an `anyuid` grant.

### The arbitrary UID trap, which cost a release to find

**An image that runs perfectly under `podman run` can still fail under
OpenShift**, and the error will not mention permissions. Both images hit a
version of this on their first ever deployment; both fixes are one line in
their Containerfile, and neither is obvious from a local run.

**The API image: `useradd -d /app` makes `/app` mode `0700`.** `WORKDIR`
then reuses that directory and `COPY` puts root-owned files inside an
unreadable parent. As UID 1001 — which every local `podman run` uses,
because the image says `USER 1001` — that is invisible. OpenShift assigns
an arbitrary UID with **GID 0**, `0700` denies it the working directory,
Python's cwd entry resolves to a directory it cannot read, and
`uvicorn app.main:app` dies with:

```
ModuleNotFoundError: No module named 'app'
```

An import error that says nothing about permissions and sends you looking
at `PYTHONPATH`. The fix is Red Hat's own convention, applied after every
`COPY`:

```dockerfile
RUN chgrp -R 0 /app && chmod -R g=u /app
```

Group 0 with group permissions mirroring owner is what makes an image work
under *any* assigned UID. To reproduce locally, run as a UID that is not
the owner and put it in group 0 — `podman run --user 12345:0 <image>` — not
the plain `podman run` that passes.

**The frontend image: `ubi9/nginx-124` is an S2I *builder* image.** Its own
CMD runs `$STI_SCRIPTS_PATH/usage`, which prints "This is a S2I rhel base
image", exits 0, and never starts nginx. A Containerfile that sets no CMD
inherits that, so the container CrashLoopBackOffs with a help message and
no error at all. It needs an explicit `CMD ["nginx", "-g", "daemon off;"]`.

### Image tags, and why not `latest`

Both images default to the chart's `appVersion` — a real release. `latest`
reads as "always current" and with `pullPolicy: IfNotPresent` is the
opposite: a node that already holds a `latest` layer never pulls it again,
not on a pod delete, not on a rollout, not after a new release. A cluster
here kept a v10-era API for hours after 11.0.0 published a working one, and
deleting the pods would not have changed it. See "Image tags" above.

## Collectors (CronJobs)

Real vendor collectors run as Kubernetes `CronJob`s, one per manager
*type*, invoking `tools/run_collector.py --manager-type <TYPE>` in the
same image as the API (`Containerfile` copies `tools/` alongside `app/`
specifically so no second image is needed). See the repo root
`README.md`'s "How data actually gets in" section for the full design,
`docs/adr/0009-ucs-manager-collector.md` for how the UCS Manager data
path was built and validated, and `docs/adr/0014-ucs-central-multi-
domain-collector.md` for how the Cisco collector drives it per domain.

A collector's entire connection config is one endpoint and one login per
manager type, set in `collectors.<vendor>` in `values.yaml`. There are no
`Manager` documents to create first and no credentials volume to mount.

`collectors.timeZone` (default `Asia/Jerusalem`) sets every CronJob's
`spec.timeZone` (Kubernetes 1.27+), so a schedule like `"0 2 * * *"` fires
at 2am local time, DST included, rather than 2am on whatever timezone the
cluster's `kube-controller-manager` happens to run — almost always UTC,
regardless of where the cluster physically sits. Set it to `""` to fall
back to the cluster default instead.

`collectors.ucsManager` is the one carve-out and has no `ip` at all: the
UCS Central collector reads every domain's address from Central at
runtime (`ComputeSystem.address`) and logs into each one with
`collectors.ucsManager.username`/`.password`, so that account has to
authenticate against every registered domain. There is no UCS Manager
CronJob to enable.

Those values render into a single `Secret`
(`templates/collector-credentials-secret.yaml`) and reach the pod as
`INVENTORY_*` environment variables via `envFrom`.

**Do not commit real passwords to `values.yaml`.** Pass them at install
time (`--set collectors.ucsManager.password=...`), from a values file kept
out of git (`-f secrets.yaml`), or — for production — set
`collectors.existingSecret` to a Secret managed by Vault, External
Secrets or sealed-secrets, which makes the chart skip rendering its own.

An externally managed Secret is consumed with `envFrom`, so its keys are
environment variable names and the chart cannot validate them — a typo
surfaces as "not configured" at collector runtime, not at install. For
the Cisco collector the required set is:

```
INVENTORY_UCS_CENTRAL_IP
INVENTORY_UCS_CENTRAL_USERNAME
INVENTORY_UCS_CENTRAL_PASSWORD
INVENTORY_UCS_MANAGER_USERNAME     # no _IP — see the carve-out above
INVENTORY_UCS_MANAGER_PASSWORD
```

Other vendors follow the `INVENTORY_<TYPE>_IP`/`_USERNAME`/`_PASSWORD`
shape; `templates/collector-credentials-secret.yaml` is the full list.

`envFrom` a Secret rather than inline `env` values is deliberate:
`kubectl get cronjob -o yaml` and `kubectl describe pod` both print plain
`env` values to anyone who can read workloads in the namespace, while a
`secretRef` shows only the reference.

`UCS_CENTRAL` covers the whole Cisco fleet — every domain registered with
Central, read through that domain's own UCS Manager. `OPENMANAGE`,
`INTERSIGHT`, `ONEVIEW` and `REDFISH_STANDALONE` each have a CronJob of
their own, all shipped disabled.

`collectors.fake` is the sixth CronJob and the one that reaches no vendor
at all: it runs `tools/seed_inventory.py`, for a cluster with no UCS,
OneView, OME, Intersight or BMC to talk to — a demo, a UI environment, or
a soak test of the ingest path itself. It is not a shortcut around the
pipeline; the seeder drives the same `ProviderServer` -> classify ->
health-evaluate -> audit -> upsert path a real collector does.

`count` and `seed` together decide the generated fleet field for field, so
repeated runs at the same pair upsert the same servers, which is what
makes it safe to schedule. **Changing either against a populated database
reports errors rather than replacing the fleet** — servers correlate on
`(vendor, serial)`, so a new seed is a second fleet. Wipe the database
first. And never enable it alongside a real collector: one estate, two
sources of truth.

`ONEVIEW` is one appliance like the rest. Power supplies and CPU thread
counts are its two potentially-per-server costs — each tried the cheap
way first (most servers' bulk sweep already carries both), falling back
to one request per server for whatever it doesn't. Every other collector
reads both out of a response it already fetches for other reasons;
OneView is the only one where either can cost something extra.
`collectors.oneview.collectPsus: false` and
`collectors.oneview.collectCpuThreads: false` turn each off
independently — the rest of the sweep is three bulk calls either way.

Note that Intersight's three fields mean something different: it signs
requests with an API key rather than logging in, so `username` is the API
Key ID and `password` the secret key.

### `REDFISH_STANDALONE`'s inventory

Unlike every other collector, this one's fleet list is a file
(`docs/examples/redfish-inventory.example.toml` shows the shape), not
values you `--set`, because at a few hundred hosts it doesn't fit
`values.yaml` or `--set` sanely. `collectors.redfishStandalone` offers the
same choice `collectors.<vendor>.password` above already does for
credentials:

- Leave `inventoryToml` blank (the default) and provision the
  `<release>-redfish-inventory` ConfigMap yourself — `kubectl create
  configmap <release>-redfish-inventory
  --from-file=inventory.toml=./redfish-inventory.toml`, or your own
  GitOps tooling. This chart never creates or touches that ConfigMap.
- Set `inventoryToml` (a multi-line string) and this chart renders and
  owns the ConfigMap instead — no separate `kubectl` step. For Argo CD
  this is the natural fit: `spec.source.helm.values` on the Application
  is inline YAML anyway, so the fleet list lives in whatever repo that
  Application manifest does. Give it the same access control as any
  other GitOps-committed config: it names every BMC and decides which
  credential each one receives, which is why the example file calls it
  "equivalent to write access to the credential Secret" even though it
  holds no password itself.

## Current state

The backend API and the frontend both have full manifests
(Deployment/Service/Route, plus the API's ConfigMap). The frontend gap
this section used to record is closed as of chart 0.2.0.

CI does now build and publish both images to GHCR on every push to main
(`docs/adr/0010-image-publishing-and-versioning.md`), but nothing
*deploys* them: there is no CD/GitOps wiring and no automatic manifest
update, tracked as pending work in `CLAUDE.md`.
