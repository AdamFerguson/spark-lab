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
    ssh: luna.tail9d5411.ts.net
    remote: true            # manage over SSH unless we are on it (see below)
    monitoring: {instance_label: luna}
    images: {litellm: docker.litellm.ai/berriai/litellm:main-stable}
  - name: sol
    ssh: sol.tail9d5411.ts.net
    remote: true
    monitoring: {instance_label: adam-spark}
    images: {litellm: ghcr.io/berriai/litellm:v1.99.0-dev.2}

models:
  qwen38-27b:
    active: true
    hosts: [luna, sol]      # where it is served — this IS the scale
    ...model fields...
    host_overrides:
      luna: {litellm: {model_name: Qwen3.8-27B-NVFP4, model_info: {...}}}
      sol:  {litellm: {model_name: adam-spark-qwen3-8-27b, model_info: {...}}}

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
`ssh` host (full + first label, so `luna.tail9d5411.ts.net` matches a box
named `luna`). Match → **local** runtime, even when `remote: true`.
`remote: false` is unconditionally local. Otherwise → Fabric `RemoteRuntime`
(one connection per remote host per run). This is what makes the identical
config work on the laptop and on every Spark.

### Fan-out

`--hosts a,b` (any host-targeted command) selects a subset; unset = all
hosts. Commands run once per selected host with sectioned output
(`==> [luna] adam@luna (remote)`), continue past a per-host failure, and
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
