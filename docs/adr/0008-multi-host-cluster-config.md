# ADR 0008: One config for the whole cluster — host fan-out and model scaling

**Status:** Accepted (implemented on branch `cli-simplify`)

**Date:** 2026-08-27

**Supersedes (partially):** the per-node remote-operator config layout from
`docs/REMOTE_OPERATOR_MODE.md` (one `install.remote` block per node, one config
file per host under `labs/<node>/`). The *mechanism* of that doc (Fabric-based
`RemoteRuntime`, node-side state, `bash -lc` login shell, detached model launch)
is kept; what changes is the *config shape* around it.

## Context

Remote operator mode (2026-08-26/27) proved that a laptop can converge a remote
Spark over SSH. Its config shape — one config file per node, each pointing at
exactly one `install.remote` host — worked, but it multiplied with the number
of nodes: every host got its own `labs/<node>/config.yaml` + `.env`, and the
operator had to remember which file drove which node.

Two requirements made the per-node shape untenable:

1. **One config for the entire cluster.** The operator wants a single
   `config.yaml` describing every managed node, driven by `--hosts`
   selection.
2. **The same file must work from a Spark itself.** Running `spark-lab
   status` on `luna` with the cluster config must converge `luna` locally
   (no SSH to itself) while still reaching `sol` remotely — the "I'm already
   here" case.

A third, related need: **scaling a model across hosts** — declaring that model
X is served on hosts A and B (with per-host tuning), and scaling it up or down
by host, as a first-class operation.

## Decision

### Schema v3

A `version: 3` config is one document for the whole cluster:

```yaml
version: 3
install:
  name: spark-lab
  install_dir: ~/AI          # cluster-wide default (per-host overridable)
  repo_url: ...             # optional: what `init --hosts` clones onto a bare node

hosts:
  - name: luna
    ssh: luna.tailnet.example
    remote: true            # manage over SSH unless we are on it (see below)
    monitoring: {instance_label: luna}
    images: {litellm: docker.litellm.ai/berriai/litellm:main-stable}
  - name: sol
    ssh: sol.tailnet.example
    remote: true
    monitoring: {instance_label: spark}
    images: {litellm: ghcr.io/berriai/litellm:v1.99.0-dev.2}

models:
  qwen38-27b:
    active: true
    hosts: [luna, sol]      # where it is served — this IS the scale
    ...model fields...
    host_overrides:
      luna: {litellm: {model_name: Qwen3.8-27B-NVFP4, model_info: {...}}}
      sol:  {litellm: {model_name: my-spark-qwen3-8-27b, model_info: {...}}}

active_models: [qwen38-27b]
litellm: {...}
monitoring: {...}
network: {...}
```

Rules:

- **Host entry keys.** `name` + `ssh` + `remote` describe the connection
  (`ssh` may be `user@host`; `remote: false` marks a host as this machine).
  *Every other key in the entry* is a config override, deep-merged over the
  cluster-wide document for that host (e.g. `monitoring:`, `images:`,
  `install: {repo_dir: ...}`).
- **Model hosting.** `models.<m>.hosts` names the serving hosts (absent = all
  hosts; `[]` = scaled down everywhere). A host view only considers models
  that serve it; a host with none converges control-plane only (no recipe
  file, no model workload).
- **Per-host model tailoring.** `models.<m>.host_overrides.<host>` deep-merges
  over that model's own fields (params, image, litellm serving identity, ...).
  This is "a particular sparkrun recipe tailored to a host".
- **One active model per host.** Two active models sharing a host is a config
  error (the single `litellm.model_name` per gateway makes a second active
  model un-servable anyway). Multiple active models with *disjoint* host sets
  are valid.
- **Removed in v3.** `install.remote` (replaced by the host entry) and
  `install.hosts` (sparkrun placement: a `min_nodes: 1` model always runs on
  the host being converged, so sparkrun gets `--hosts 127.0.0.1` there;
  multi-Spark TP placement is the ADR-0007 follow-up and is rejected with a
  clear error until then).
- **Secrets.** One `.env` next to the cluster config. Per-host-different values
  use per-host env-var names via overrides (e.g. sol's
  `litellm: {master_key_env: LITELLM_MASTER_KEY_SOL}`); shared values keep
  one name.

### Per-host views

`Config.view_for(host)` returns a full `Config` built from
`deep_merge(base, host overrides, model host_overrides)`. Render, plan,
converge, and state therefore run *per host view* without any of them knowing
a cluster exists — the engine stays single-node-shaped, which is what keeps
the v1/v2 path byte-identical (goldens unchanged).

### Local auto-detection

For each selected host, the tool matches this machine's identities (hostname,
`/etc/hostname`, FQDN aliases, primary IP) against the entry's `name` and
`ssh` host (full + first label, so `luna.tailnet.example` matches a box
named `luna`). Match → **local** runtime, even when `remote: true`.
`remote: false` is unconditionally local. Otherwise → Fabric `RemoteRuntime`
(one connection per remote host per run). This is what makes the identical
config work on the laptop and on every Spark.

### Fan-out

`--hosts a,b` (any host-targeted command) selects a subset; unset = all
hosts. Commands run once per selected host with sectioned output
(`==> [luna] you@luna (remote)`), continue past a per-host failure, and
return non-zero if any host failed. **State stays per node** (the node-side
`<repo_dir>/.sparklab-state/state.json`, exactly as before) — there is no
cluster-level state.

### Model scaling

`models.<m>.hosts` is the scale:

- `spark-lab model up <m> [--hosts x]` — add host(s) to the model's `hosts:`
  (all remaining hosts when `--hosts` is unset), mark the model active, and
  converge those hosts. Starting is routine: the ensure path starts what
  isn't running.
- `spark-lab model down <m> --yes [--hosts x]` — remove host(s), converge
  them, which stops the workloads that are no longer current (stale-recipe
  stops pass the gate deliberately). Fully scaled down: `hosts: []` +
  `active` dropped.
- `spark-lab model stop --yes` (legacy) — stop now, config unchanged; a
  routine `apply` restarts it.

Up/down rewrite `config.yaml` on disk (YAML round-trip: comments in the file
are not preserved — keep config comments minimal).

### CLI surface (consolidated)

```
spark-lab init [--yes]                     # create config.yaml + .env (v3 example)
spark-lab init --hosts a,b [--yes] [--all] # idempotent host bootstrap
spark-lab status [--hosts a,b]
spark-lab apply [--hosts a,b] [--dry-run] [--diff] [--restart-model]
spark-lab model up <m> [--hosts a,b]
spark-lab model down <m> --yes [--hosts a,b]
spark-lab model stop --yes [--hosts a,b]
spark-lab teardown --yes [--purge] [--hosts a,b]
spark-lab upgrade --yes [--hosts a,b]
spark-lab check [--images] [--probe] [--system] [--install] [--all] [--hosts a,b]
spark-lab logs <service> [--hosts <one>] [-f]
spark-lab migrate [--dry-run]              # v1/v2 -> v3
spark-lab adopt [--dry-run] [--hosts a,b]
spark-lab recipes {search,list,show,convert}   # unchanged (host-agnostic)
# hidden aliases: validate == check; doctor == check --system; apply --apply/--yes
```

Gating convention: destructive ops keep `--yes` (`teardown`, `model down`,
`upgrade`); `apply`'s model-restart gate is the self-explanatory
`--restart-model` (replacing `--apply`); `model up` is not gated (starting a
model is routine, and the conflict check + converge are idempotent).

### `init --hosts` (host bootstrap)

Idempotent preparation of a (new or existing) managed node, report-only
without `--yes`: install the required tools (`check system --install`
semantics), ensure the spark-lab git checkout on the node (clone from
`install.repo_url` when missing; `fetch` + `pull --ff-only` when the tree is
clean; skip + report when dirty), ensure `install_dir`, and bring tailscale up
(best-effort). Safe to re-run any time — new dependencies just get installed.

## Migration

`spark-lab migrate` rewrites a v1/v2 config to v3: it preserves every value
(chained `upgrade_to_v2` + `upgrade_to_v3`) and synthesizes the single
host entry from `install.name` + `install.remote` (when present). The operator
cluster config (all hosts, per-host overrides) is assembled once from the
per-node configs; the same file is then used on every machine (laptop and
each Spark).

## Consequences

- Per-node config files under `labs/<node>/` are obsolete; the cluster config
  + one `.env` replace them (removed after the live migration).
- `config.yaml` is now genuinely shared infrastructure: it must be identical
  on the laptop and every node (state is still per node, so no divergence
  hazard in the other direction).
- Node-to-node ssh keys are only needed for all-hosts commands *run from a
  node*; self-targeting commands from a node always work (auto-detection).
- v1/v2 configs keep working unchanged (legacy single-host path, byte-
  identical goldens).
- **Open follow-ups (not this ADR):** multi-Spark TP placement
  (ADR-0007), optional `init --mesh` (node-to-node key exchange via the
  operator's connections), `recipes` as an agent-driven discovery/optimization
  tool.

## Addendum (2026-08-28): monitoring roles

The observability stack can now be split across the cluster per host via
`monitoring.role` (a per-host override like any other):

- `full` (default) — prometheus + grafana + the exporter sidecars, as before.
- `exporters` — only the exporter sidecars (node/dcgm/cadvisor/gpu-textfile).
  A `full` host's prometheus scrapes every `exporters` host over its `ssh`
  address (Tailscale/LAN DNS), tagging the metrics with that host's
  `instance_label` so the existing dashboards work across the cluster. This
  is how "monitor luna from sol" works: one central Grafana, exporters
  everywhere.
- `none` — no monitoring services (equivalent to the legacy
  `monitoring.enabled: false`).

Rendered-file consequences: an `exporters`/`none` host's render drops
`prometheus.yml` + the grafana provisioning/dashboards (converge deletes them
on transition), and its compose only carries the enabled services/volumes.
Legacy v1/v2 configs render byte-identically (the remote-scrape loop is empty).

Design notes:
- The scrape address is the host's `ssh` value — the same address operators
  already use, so no new networking knob.
- Remote jobs are named `sglang_<host>` / `node_<host>` / `dcgm_<host>` /
  `cadvisor_<host>`; dashboards select hosts by the `instance` label, which
  is set explicitly per job, so job-name suffixes are irrelevant to queries.
- The `sglang_<host>` remote job only renders when that host actually serves a
  model (its model `port` is present).
- Unchanged: exporters bind 0.0.0.0 on the host, so tailnet/LAN reachability
  is all that's required (the node firewall, if any, is out of scope).

## Addendum (2026-08-28): control-plane split + volume-destruction invariant

The LiteLLM control plane (gateway + Postgres + Redis) is now a per-host
override like `monitoring.role`, via `control_plane: {enabled: <bool>}`
(default `true`):

- `enabled: false` makes a host observability-only: the compose omits the
  `litellm`/`db`/`redis` services and their `postgres_data`/`redis_data`
  volume declarations, and the render skips `litellm/config.yaml`,
  `litellm/model_config.yaml` and `litellm/.env` (the gateway's own files).
  `docker compose up -d --remove-orphans` stops the now-absent containers on
  transition; the named volumes stay on disk (Docker never deletes named
  volumes on `up`), so re-enabling + `apply` restores the previous database.
- Invariants (enforced by `validate`/`check`, and `model up` refuses the
  target host):
  1. a host that serves the active model must keep the control plane on —
     the gateway is how that model is served;
  2. a host with the control plane off must still run something
     (`monitoring.role != none`), otherwise it would converge to an empty
     stack.
- Legacy v1/v2 configs are unaffected (the flag defaults on; renders are
  byte-identical — the sha256 goldens prove it).

**Volume-destruction invariant:** the named volumes
(`litellm_postgres_data` — the LiteLLM/Postgres DB —, `litellm_redis_data`,
`litellm_prometheus_data`, `litellm_grafana_data`) are destroyed **only** by
`spark-lab teardown --yes --purge` (`docker compose down -v`). No other
command — `apply`/converge reconcile, `model up/down/stop`, disabling the
control plane — deletes them. `teardown --yes` (default) keeps them, and
`--purge` prints an explicit, per-volume warning naming the database as
unrecoverable before proceeding.

## Addendum (2026-08-28): implicit central serving (`model_list` beyond the local host)

A host's role is now cleanly split into **run** vs **serve**:

- **run** — `models.<m>.hosts` (unchanged): the recipe renders there,
  `sparkrun run --ensure` + the bounded readiness probe target it, and stale
  stops happen there.
- **serve** — *implicit, no new key*: **every active model with at least one
  running host is registered in the `model_list` of every control-plane host.**
  The entry's `api_base` points at the gateway's own engine when the model
  runs there (`model_api_base_host`, default `host.docker.internal`) and at
  the first running host's `ssh` address (Tailscale/LAN DNS) otherwise.

This is what makes "the model runs on luna, the API lives on sol" a config
change, not a networking project: with luna control-plane-off, sol's gateway
registers the model under its existing serving identity pointing at
`http://luna:<port>/v1`, and clients keep hitting `sol:<gateway_port>` with the
same model name.

Design notes:
- **Serving identity** resolves per entry: gateway-litellm base < model
  `litellm:` block < `host_overrides.<gateway>.litellm` (last wins). A model's
  per-host `host_overrides.litellm` no longer merges into the view-wide
  gateway section — it only shapes that host's entry.
- **Escape hatch**: an explicit `litellm.api_base` (model-level or per gateway
  host) overrides the derived address (load-balancer front, alias, non-
  tailnet addressing). Multi-target entries under one name (true cross-host
  load balancing) are a follow-up.
- **Scaled-down models register nowhere** (no running hosts ⇒ no entries); a
  gateway with zero entries renders `model_list: []` and boots model-less.
- **Invariants** (replacing the earlier "model host keeps its control plane"):
  a control-plane-off host may RUN the model freely; a cluster must have at
  least one CP-on host whenever active models run; and two active models must
  not resolve to the same serving `model_name` on one gateway (LiteLLM refuses
  duplicate names at boot).
- **Litellm hot-swap**: a changed tracked `litellm/model_config.yaml` or
  `litellm/config.yaml` triggers a best-effort `docker compose restart
  litellm` (same bug class as the prometheus hot-reload — a running gateway
  keeps its boot-time model list, and `up` does not recreate it). This is
  what makes entry flips (local → remote, add, remove) take effect.
