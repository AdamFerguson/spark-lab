# ADR 0007: Cluster control plane — one LiteLLM, N model nodes, all-node metrics

**Status:** Proposed
**Date:** 2026-08-26
**Supersedes:** parts of ADR 0002 (runtime seam is unchanged; this extends *where* the
observability + gateway services live in a multi-node cluster).

## Context

`spark-lab` is built around a **single control plane**: `apply` renders one
`docker-compose` (LiteLLM + Postgres + Redis + Prometheus + Grafana + the
host/GPU metric agents) and runs it on **one** node's `install_dir`. That is
correct for a single Spark.

Two Sparks change this. In a two-node cluster we want:

1. **One control plane.** LiteLLM (the API gateway + spend-tracking DB) and the
   monitoring stack run on **exactly one** Spark. The other Spark is a **worker**
   that only runs the model (its shard, via the sparkrun SSH mesh) and — for
   observability — the metric agents. No worker runs LiteLLM/Postgres/Grafana.
2. **Metrics for both hosts in Grafana.** The user wants per-host GPU/host
   dashboards for *every* node, not just the control plane. (Chosen **Tier 2**:
   the control plane deploys the metric agents to the workers too.)
3. **A configurable model API endpoint.** The model's primary API endpoint may
   live on a *different* node than the control plane. E.g. control plane on one
   node, but the model's serving endpoint on another — LiteLLM must be able to
   point at whichever host serves it.
4. **Multi-host dashboards without fragility.** Dashboards must cover all hosts
   without turning the (large, `{{`-heavy) Grafana JSON into Jinja templates.

These are independent of the *model-distribution* mechanics in
`CLUSTERING.md` (which still require on-hardware verification of the sparkrun
multi-node CLI). This ADR is about **where the control plane + observability
live and how they're configured**, which is node-independent and can be designed
and unit-tested now.

## Decision

### 1. Node roles

| Role | Services | Nodes |
|---|---|---|
| **Control plane** | LiteLLM, Postgres, Redis, Prometheus, Grafana + metric agents + model shard | **exactly 1** |
| **Worker** | model shard (via sparkrun) + metric agents | N |

- `install.control_plane` names the control-plane node. It **must** be a member
  of `install.hosts`. Default (when omitted): the **first** host.
- The control plane is where spark-lab runs + writes its local `install_dir`.
- Workers run only the metric agents (deployed by the control plane, §4) and the
  model shards (distributed by sparkrun, `CLUSTERING.md`).

### 2. Split the compose into two, by role

The single `docker-compose.yaml.j2` is split into **two rendered files**:

- **`stack.yaml.j2`** — the **control plane**: `litellm`, `db`, `redis`,
  `prometheus`, `grafana`. Rendered + deployed **only on the control plane**.
- **`metrics.yaml.j2`** — the **metric agents**: `node_exporter`,
  `dcgm_exporter`, `cadvisor`, `gpu_textfile`. Rendered + deployed on **every
  node** in `install.hosts` (control plane + workers).

All metric-agent services are **port-mapped to the host** (9100, 9835, 8080,
9161) so they are reachable from the control plane's Prometheus by hostname,
regardless of node (§5). This makes scraping **uniform**: the control plane's own
agents and the workers' agents are scraped the same way (by `hostname:port`),
so there is no special case for "the local node's docker service name."

> Convergence: the control plane writes its local `stack` + `metrics` into its
> `install_dir` and converges them locally. For workers, it converges **remotely**
> (§4). `state.json` records both local and remotely-converged files.

### 3. Configurable model API endpoint

Add `model.api_base_host` (default **`host.docker.internal`**). LiteLLM's gateway
reaches the model at:

```
http://{ model.api_base_host : model.port }
```

- **Default** (`host.docker.internal`): the model's primary endpoint is on the
  control plane itself (the common case; `host.docker.internal` → host-gateway).
- **Cross-node**: set `model.api_base_host` to the tailnet hostname of whichever
  node hosts the model's *primary serving endpoint* (e.g. a worker). Then the
  control plane's LiteLLM proxies to that host:port over the mesh network.

This flows into the rendered `litellm_model_config.yaml` (`api_base`) and
`litellm.env` (the `LITELLM_API_BASE`/model base URL), keeping the secret
in `.env` and only the host/port in the config (never a credential).

### 4. Who deploys the worker metric agents (Tier 2)

The control plane is the **single source of truth**. After the sparkrun SSH mesh
exists (`sparkrun setup ssh --hosts ...`), the control plane converges the
**metrics** file onto each worker over SSH:

1. Render `metrics.yaml` + the metric-agent `.env` locally.
2. For each non-control-plane host: `scp`/`rsync` the file(s) into a
   `~/.sparklab-metrics/` (worker-side) dir, then run `docker compose up -d`
   **remotely** (over the established SSH).
3. Record the remote deploy in `state.json` (host → file sha256 + "running") so
   re-`apply` is convergent + idempotent, and so `teardown` can reach back and
   `docker compose down` the worker's metric agents.

The **model** is *not* deployed by spark-lab this way — sparkrun distributes
model shards (`CLUSTERING.md`). spark-lab's remote converge is **only** for the
metric agents. This keeps the blast radius small + the SPOF scoped to
observability/management, not the model.

> **Remote-deploy primitive**: this needs a small "run this command / copy these
> files on host X" capability that spark-lab gains (wrapping the existing
> `run`-over-SSH seam, ADR 0002). It is the new piece this ADR introduces to
> `converge`. It is read-only-safe: it only ever manages the metric-agent
> compose on workers, never the model.
>
> **Status note (2026-08-26):** the remote primitive has now shipped, in a
> *generalized* form, as **remote operator mode** (`install.remote` +
> `sparklab/core/remote.py`): any command (`apply`, `status`, `teardown`,
> `upgrade`, `adopt`, ...) can converge a remote node over SSH (Fabric) from an
> operator machine, with state kept on the managed node. See
> `docs/REMOTE_OPERATOR_MODE.md`. What remains from this ADR is the
> control-plane/worker *role split* (metric-agent deploy per host, multi-node
> Prometheus, host-aware dashboards) — the remote primitive above is the
> foundation it builds on.

### 5. Multi-node Prometheus (templated)

`prometheus.yml.j2` becomes **target-aware of all hosts**. For each node in
`install.hosts` (control plane + workers), it emits scrape targets for:

- `node` (node_exporter) → `<host>:9100`
- `dcgm` (dcgm_exporter) → `<host>:9835`
- `cadvisor` → `<host>:8080`
- `sglang` (model metrics, only for nodes that host the model) →
  `<model-host>:<model.port>`

Every target is labeled with **`host: <hostname>`** (and a distinct
`instance`). The control plane scrapes its own agents the same way (by its own
hostname), so the config is uniform. `external_labels.host` stays per-instance
at scrape-label level (not a single global value).

### 6. Multi-host Grafana dashboards (static, host-aware — not Jinja)

**The dashboards are NOT Jinja-templated.** Reason: Grafana dashboard JSON is
large and full of `{{`/`}}` (Grafana's own expressions) that collide with Jinja
delimiters; templating it is brittle and unmaintainable. Instead:

- Each dashboard gets **one Grafana variable** `$host`, of type **query**, with
  `query = label_values(up, host)` (or `label_values(node_load1, host)`),
  `multi = true`, `includeAll = true`, default "All". This auto-populates the
  host dropdown from whatever the `host` label is in Prometheus (§5).
- Panels add a **`host=~"$host"`** filter to their PromQL (or are written
  host-agnostic / as per-`host` series). Selecting a host filters; "All"
  aggregates or shows one row per host.

**Which dashboards change:**
- `spark-host-overview.json` → rename concept to *Host Overview*; add `$host`
  + `host=~"$host"` to its node/dcgm/cadvisor panels. It becomes per-host (pick
  a Spark, see its GPU/RAM/CPU).
- `sglang-dashboard.json` → add `$host` scoped to the `sglang_*` series so, when
  the model spans nodes, you can view the engine metrics per node (or aggregate
  if the engine labels them per-node).
- Both stay **static `.json`**, shipped as today; only their contents gain the
  variable + label filter.

> This directly answers "can multi-host be templated in spark-lab?": the
> **scrape side** is templated (per-host targets + `host` labels in
> `prometheus.yml.j2`); the **presentation side** is static-but-parameterized via
> a Grafana variable. No dashboard JSON passes through Jinja.

### 7. Config surface (v2 additions)

```yaml
version: 2
install:
  name: lab
  install_dir: ~/AI
  cluster_name: lab
  control_plane: node-a            # runs LiteLLM + Postgres + Prometheus + Grafana
  hosts:
    - node-a                       # control plane (also a model node here)
    - node-b                       # worker
models:
  qwen:
    active: true
    runtime: sglang
    hf_model: "..."
    image: lmsysorg/sglang:latest
    port: 30000
    min_nodes: 2                   # model spans both nodes
    node_assignment: [node-a, node-b]   # which nodes host the model (or "auto")
    api_base_host: node-b          # LiteLLM -> http://node-b:30000 (model primary here)
    resources:
      mem_fraction_static: 0.85
    params:
      tensor_parallel_size: 2
    extra_flags: [ --enable-metrics ]
monitoring:
  enabled: true
  workers_metrics: true            # Tier 2: deploy metric agents on every host
```

- `install.control_plane`: the control-plane host (must be in `hosts`; default =
  first host). **NEW.**
- `model.node_assignment`: which hosts host the model (list, or `auto`); the
  control plane may or may not be one. **NEW** (was implicit from `min_nodes`).
- `model.api_base_host`: where LiteLLM reaches the model's primary endpoint
  (default `host.docker.internal`). **NEW.**
- `monitoring.workers_metrics`: when `true` (default in a multi-host config),
  metric agents run on every host; when `false`, only the control plane is
  monitored (Tier 1).

## Alternatives considered

- **Per-host Jinja-templated dashboards (render one JSON per node).**
  *Rejected:* Grafana JSON collides with Jinja delimiters; N large templates are
  brittle + hard to maintain. A static host-aware dashboard + a `$host` variable
  is simpler and covers any host count.
- **Tier 1 only (no worker metrics).** *Rejected:* the user wants both hosts in
  Grafana. Tier 2 adds the worker metric agents; Tier 1 is still available via
  `monitoring.workers_metrics: false`.
- **Model endpoint auto-discovered.** *Rejected:* explicit `api_base_host` is
  clearer, matches the "primary endpoint may be on any node" requirement, and
  avoids a discovery step. Auto-detection can be a later refinement.
- **Each node runs its own LiteLLM (per-node gateway).** *Rejected:* contradicts
  "one control plane"; a single gateway + single Postgres is the point (one place
  to key, spend-track, and monitor).

## Consequences

- **Control plane is a SPOF for the API + monitoring.** If it dies, the model
  keeps serving on the workers (sparkrun-managed) but LiteLLM + Grafana are down
  until `apply` is re-run on the controller. Accepted for a 2-node lab.
- **The control plane manages workers remotely.** It needs the SSH seam to push
  the metric-agent compose + run `docker compose` on workers. Blast radius is
  scoped to observability; the model is unaffected by a control-plane outage.
- **Prometheus scraping is uniform** (every node by `hostname:port`, `host`
  label), which keeps `prometheus.yml` + the dashboards consistent at any host
  count.
- **New remote-converge primitive** in `converge` (copy + run `docker compose`
  on a remote host) — the one genuinely new code path this ADR adds, beyond
  re-rendering + the config fields.
- **Config stays secret-safe:** `api_base_host` + hostnames are topology, not
  credentials; they live in config, never in the rendered gateway/secret files.
- **Swappable + migration-friendly control plane.** `install.control_plane` makes
  the control plane explicit + movable, which supports the *bridge-then-migrate*
  pattern: stand up an interim control plane on a second node, point dependent
  sessions at it, then migrate the original node (which keeps its data and can
  remain the eventual control plane). When converging an **existing** install, the
  rendered compose must **reuse the existing named Docker volumes** (Postgres /
  Redis / Grafana / Prometheus) so their data survives — the compose split (§2)
  keeps volume names stable across the split.

## Open questions (resolve on the two Sparks — see CLUSTERING.md)

1. **Model primary-endpoint semantics**: when the model spans N nodes via
   sparkrun, which node is the *primary serving endpoint* that LiteLLM should
   target? (Assume sparkrun designates one; confirm + map to `api_base_host`.)
2. **Worker metric-agent remote deploy**: exact `scp`/`docker compose` invocation
   over the mesh; where the worker-side state lives; idempotency on re-apply.
3. **`node_assignment` vs `min_nodes`**: does sparkrun take an explicit node
   list, or just a count? Confirm + align `node_assignment` to whatever it
   supports.
4. **Cross-node scrape reachability**: confirm Prometheus (on the control plane)
   reaches each worker's 9100/9835/8080 over the mesh network (should, via
   tailscale/LAN).
5. **`sglang` per-node labeling**: when the engine runs on multiple nodes, does
   each node's `/metrics` carry a distinguishing label so the dashboard can
   split them by `host`? Confirm the exact metric label.
