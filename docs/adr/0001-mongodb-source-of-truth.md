# ADR-0001: MongoDB as the inventory system of record

## Status

Accepted

## Context

The inventory must hold structurally heterogeneous documents: a Dell
`PowerEdge` and a Cisco UCS blade share almost no hardware fields in
common, `connectivity.attachments` is a variable-length list that some
vendors won't populate at all, and the schema will keep growing as new
managers are added — all without a maintenance-window schema migration.
PostgreSQL's `JSONB` support has closed much of the historical gap for
this kind of workload, and is the stronger default choice for most
applications that only have a *small* amount of unstructured data
alongside a mostly-relational core.

## Decision

Use MongoDB as the system of record. Comparative analysis of both engines
for CMDB-style, heterogeneous-document workloads specifically (not
general-purpose OLTP) continues to favor a native document store: nested,
variable-shape, per-vendor hardware and connectivity data is the entire
shape of this domain, not an incidental part of it, which is precisely the
case where MongoDB's advantage over JSONB-in-Postgres persists.

## Consequences

- No relational joins; cross-collection references (`site_id`,
  `manager_id`) are resolved in the application layer.
- Schema discipline is enforced by the application (Pydantic models,
  `schema_version` fields, migration tooling), not the database — MongoDB
  will accept a malformed document if the app lets one through.
- Multi-document transactions are avoided by design (see `app.errors`'s
  optimistic-concurrency model) rather than assumed available, keeping the
  option open to run without a replica set in the smallest deployments.
