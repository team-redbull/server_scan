# ADR-0012: One endpoint and login per manager type, from the environment

## Status

Accepted. **Partially superseded by ADR-0014's 2026-08-17 update**, which
carves out one exception to the rule this ADR establishes: `UCS_MANAGER`
now has a login but no endpoint. Its domains are reached by the UCS
Central collector at the addresses Central reports for them, so
`INVENTORY_UCS_MANAGER_IP` — used as the headline example below — no
longer exists. The one-endpoint-and-login-per-type shape still holds for
every other manager type, and the reasoning for it is unchanged.

## Context

A collector's connection details were split across two places. The
endpoint and a `credential_ref` lived on a `Manager` document in MongoDB;
the username and password lived in a Kubernetes `Secret`, projected into
the pod as `{credentials_dir}/{credential_ref}/{username,password}` and
read by `FilesystemCredentialResolver`.

Onboarding a vendor therefore meant: create a Secret, add a `sources`
entry to the CronJob's projected volume so it lands under the right
subdirectory, and insert a `Manager` document into MongoDB whose
`credential_ref` matched that subdirectory name. Three artifacts in three
systems, with the only thing tying them together being a string that had
to agree in all three. Nothing verified it did; a mismatch surfaced as a
`CredentialNotFoundError` at collection time.

The file-based design was chosen originally because a Secret volume keeps
credentials out of the pod spec, and because a CronJob's manifest would
not need editing every time a manager was added. Both are real
advantages. But they were paying for flexibility this platform does not
use: there is one UCS Manager, one OneView appliance, one OME. UCS
Manager's own multi-domain story is the `UCS_CENTRAL` parent enumerating
its domains at collection time (see `Manager`'s docstring), not a
hand-maintained list of endpoints.

Separately, `deploy/` held two equivalent manifest sets — plain OpenShift
YAML and a Helm chart — for the same five resources, with nothing
checking that they agreed.

## Decision

**One endpoint and one login per manager type, from settings.**
`INVENTORY_UCS_MANAGER_IP` / `_USERNAME` / `_PASSWORD`, and the same
shape for `ONEVIEW`, `OME`, `UCS_CENTRAL` and `INTERSIGHT`. That is the
whole of a collector's connection config. `FilesystemCredentialResolver`,
the `credentials_dir` setting and `Manager.credential_ref` are all
removed.

Resolution is keyed on `ManagerType`
(`EnvConnectionResolver.resolve(manager_type)`), not on a per-manager
reference. Onboarding a vendor is filling in three values, with no
document to create first and nothing that can disagree with itself.

A `Manager` document is still written on each run
(`tools.run_collector.manager_for`), with a deterministic id, so the API
and UI can resolve `Server.manager_id` to something readable. It is a
*projection* of configuration, never its source.

**A half-configured vendor is a configuration error**, raised as
`ManagerNotConfiguredError` naming the exact variables to set. It must
not reach the vendor: a blank password is a real login attempt that comes
back as "bad credentials" and sends an operator hunting a password
problem that does not exist. `ManagerConnection.__repr__` redacts the
password so it cannot leak through a traceback or debugger frame.

**Intersight reuses the same three fields with different meanings.** It
signs each request with an API key rather than logging in, so `username`
carries the API Key ID and `password` the secret key, with `ip` being
`intersight.com` or the appliance FQDN. Keeping one shape means one
Secret and one values block per vendor instead of a special case; the
different meaning is called out in the settings, `values.yaml` and the
example Secret, because handing Intersight an account password would look
plausible and never work.

**Kubernetes: values render into one Secret, injected with `envFrom`.**
Not inline `env` values — `kubectl get cronjob -o yaml` and `kubectl
describe pod` both print those to anyone who can read workloads in the
namespace, while a `secretRef` shows only the reference. Setting
`collectors.existingSecret` makes the chart skip rendering its own Secret
entirely, so a deployment whose secrets are owned by Vault, External
Secrets or sealed-secrets keeps that ownership.

**`deploy/openshift/` is deleted; the Helm chart is the only manifest
set.** The two had already drifted — this very change had to be made
twice and landed fully in only one of them. `helm template` renders plain
YAML on demand for anyone who wants it, and both ArgoCD and Flux consume
Helm natively.

## Consequences

- **Breaking.** Deployments must set the `INVENTORY_<VENDOR>_*` variables.
  `INVENTORY_CREDENTIALS_DIR` and the credentials volume are gone, as is
  `Manager.credential_ref`.
- Environment variables are a weaker secret channel than a mounted
  Secret volume: they are visible to every process in the container and
  in a crash dump. Routing them through a Secret with `envFrom` keeps
  them out of the pod spec and out of `kubectl describe`, which is the
  bulk of the practical exposure, and `collectors.existingSecret` leaves
  the door open to a real secret store. A deployment that wants tmpfs
  isolation would need its own `CredentialResolver` implementation — the
  seam is still a `Protocol` precisely so that stays possible.
- Several managers of one vendor type are no longer expressible. That is
  the intended narrowing; the answer for multiple UCS domains is the UCS
  Central collector, not a list of endpoints.
- `deploy/README.md` no longer documents two paths, and a change to a
  manifest is made once.
