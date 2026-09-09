# Cluster membership CronJobs

Deployed **to every cluster**, by ArgoCD — not part of the Helm chart in
`deploy/`, which deploys the platform itself. These jobs run *inside* a
cluster and report which servers it is using.

Two jobs, and which ones a cluster gets depends on what it is:

| Job | Runs on | Reads | Writes |
|---|---|---|---|
| `openshift-nodes` | every cluster | its own worker nodes | `INSTALLED` + the cluster name |
| `openshift-agents` | MCE hubs only | its Agents | `INSTALLED` + cluster, or `INSTALLED_TO_INVENTORY` |

A UPI cluster gets the nodes job. An MCE hub gets both: the agents job for
the fleet it manages, and the nodes job for its own control-plane hardware.

## What a job may release

Neither job writes `AVAILABLE` for the whole fleet. Each reconciles only
the servers already naming **its own** cluster (or MCE): a server it no
longer lists is released, and one belonging to another cluster is never
touched. That is what makes per-cluster deployment safe — a broken job in
one cluster cannot free another cluster's machines.

Two refusals are built in, and both matter more than they look:

- A failed API read exits non-zero and writes nothing. A read that failed
  is not evidence of an empty cluster.
- A *successful* read returning nothing also refuses to write. A cluster
  with no workers is far more likely a broken selector, an RBAC change or
  a mid-upgrade blip than a genuinely emptied cluster.

## Per-cluster configuration

Each overlay sets, at minimum:

```yaml
INVENTORY_OPENSHIFT_CLUSTER_NAME: ocp4-tlv     # nodes job
INVENTORY_OPENSHIFT_MCE_ID: mce-tlv            # agents job
```

`INVENTORY_OPENSHIFT_EXCLUDE_NAME_PARTS` (default `infra,control-plane`)
drops nodes by name substring, *after* the
`node-role.kubernetes.io/worker` label selector. Both filters apply: infra
nodes usually carry the worker label too, so the label alone keeps them,
while names alone would misfire on a node called `compute-infra-01`. Add
to it per cluster rather than editing the code.

## What each cluster needs

- **Network** to the platform's MongoDB, plus the `mongo-uri` Secret. The
  jobs write directly, like every collector.
- The image the platform's API runs from, pullable from this cluster.
- The ServiceAccount and ClusterRole in `base/` — read-only on `nodes`
  and on `agent-install.openshift.io` agents.

## Check before trusting a run

```bash
oc create job --from=cronjob/openshift-nodes probe-1 -- \
  python3 -m tools.collect_openshift --source nodes --dry-run
```

`--dry-run` writes nothing and prints the match rate plus every unmatched
hostname. A low match rate means correlation is failing, not that the
cluster is empty — check that Dell hosts have their **requested** hostname
set, since their reported one is derived from a MAC and matches nothing.
