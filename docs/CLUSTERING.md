# Clustering the two Sparks

**Goal:** run the two DGX Sparks as one cluster so a model can span both nodes
(e.g. a model too large for one node, or to raise capacity), driven end-to-end by
`spark-lab` — and tear it back down to single-node cleanly.

This is a **plan to execute after the new Spark passes its E2E** and (optionally)
after the existing Spark has been migrated. It builds on the multi-node plumbing
that is already in `spark-lab`'s converge engine.

> **State of the art:** `spark-lab` already emits the multi-node sequence when
> `install.hosts` has >1 entry:
> - `sparkrun setup ssh --hosts <hosts>` — build the passwordless SSH mesh
> - `sparkrun cluster create <name> --hosts <hosts>` — save the cluster
> - `sparkrun run <recipe> --ensure --cluster <name>` — run the multi-node recipe
> - `sparkrun stop <recipe> --cluster <name>` / `teardown --cluster` — wind it down
>
> The **exact** sparkrun multi-node CLI + node-selection semantics are the main
> things to **verify on the real hardware** (flagged below).

---

## How multi-node works (model)

- **sparkrun** is the orchestrator. A recipe with `min_nodes: N` tells it the model
  needs N nodes. It places workloads across the nodes in the mesh (its
  `node_assignment` / scheduler decides placement) and runs the model's
  `command` on each participating node with `{host}`/`{port}`/`{model}` filled in.
- **The mesh** is passwordless SSH from the **primary/controlling node** to every
  peer. `spark-lab` drives this via `sparkrun setup ssh --hosts`.
- **One node is the controller**: it runs the sparkrun CLI, the LiteLLM +
  monitoring stack (docker compose), and the state. The peers run the model
  workload containers that the controller schedules. (Confirm the controller
  selection rule on hardware — see open questions.)

---

## Topology decision (pick one)

- **Option A — one controller, two-node model (recommended for a spanning model).**
  The primary Spark is the controller (spark-lab, LiteLLM, Grafana/Prometheus,
  state). Both Sparks join the mesh. The recipe uses `min_nodes: 2`. The model
  spans both GPUs. The peer's own spark-lab/LiteLLM stack is **not** run — it's a
  model worker node.
- **Option B — two independent single-node labs, clustered only for the model.**
  Each Spark keeps its own full spark-lab stack. Clustering is used only when you
  want a specific model to span both. More flexible for "each node also serves
  its own small model" but more moving parts.

Start with **A** — it's the cleanest for "use both Sparks as one bigger machine."

---

## Prerequisites

- [ ] Both Sparks reachable on a private network — **Tailscale** is the intended
      mechanism (both in the same tailnet, so they resolve by tailnet hostname).
      Same-LAN also works if you're in the same broadcast domain.
- [ ] Both nodes have `docker`, `sparkrun`, `git`, `python3`, `curl`
      (`spark-lab doctor` on each).
- [ ] The **controller** has the model image pulled (peers need it too if they run
      the model workload — confirm whether sparkrun pulls per-node or expects it
      pre-pulled).
- [ ] The two Sparks' hostnames/IPs are stable and known.

---

## Steps

### 1. Bring both Sparks onto the tailnet
```bash
# on BOTH Sparks
spark-lab doctor            # confirm tailscale present (install if not)
sudo systemctl enable --now tailscaled
tailscale up                # both join the same tailnet; note each node's tailnet hostname
```
Confirm reachability: from the controller, `ssh <peer-hostname>` should work
(passwordless after step 3).

### 2. Configure the cluster in the controller's `config.yaml`
On the **controller** Spark:
```yaml
install:
  name: mylab
  install_dir: ~/AI
  cluster_name: mylab          # the saved sparkrun cluster name
  hosts:                       # >1 entry => is_cluster
    - <controller-tailnet-hostname>
    - <peer-tailnet-hostname>
  # model block:
model:
  recipe_name: <model>
  min_nodes: 2                 # the model spans both nodes
  # node_assignment: <...>     # only if sparkrun supports an explicit placement hint
  # ... (image, hf_model, port, params as the single-node case)
```
`network.tailscale.enabled: true` (default) makes `apply` ensure tailscaled runs.

### 3. Preview the cluster plan
```bash
spark-lab apply --dry-run
```
You should see the cluster dance: `setup ssh --hosts ...`, `cluster create mylab
--hosts ...`, and `run <model> --ensure --cluster mylab`. **This step builds the
mesh** (when you actually run it) — the passwordless SSH from controller to peer.

### 4. Build the mesh + launch
```bash
spark-lab apply --restart-model
```
This runs, in order: ensure tailscaled → `sparkrun setup ssh --hosts` (mesh) →
`sparkrun cluster create` → `sparkrun run <model> --ensure --cluster` → reconcile
the LiteLLM/monitoring stack. Watch the mesh step succeed (passwordless SSH).

### 5. Verify
- `sparkrun status` (and `sparkrun status --cluster mylab` if that's the flag) —
  the model job spanning both nodes.
- `docker ps` on **both** nodes — the model workload containers on the peer(s).
- `spark-lab status` — the controller's stack + tailscale + (cluster) model.
- Hit the gateway: `curl <controller>:4000/v1/models` + a completion — served by
  the multi-node model.
- Grafana: the multi-node model's metrics + a per-host overview for both nodes.

---

## Rollback / de-cluster

To go back to single-node:
```bash
sparkrun stop <model> --cluster mylab      # stop the spanning model
spark-lab teardown --cluster               # or, to tear the whole controller stack
# then: in the v3 config, remove the peer from the model's `hosts:` (and min_nodes: 1), apply --restart-model
```
Confirm the exact "remove cluster" sparkrun command on hardware (may be
`sparkrun cluster delete <name>` or similar).

---

## Open questions to confirm on hardware

- [x] **Placement addresses are IPs, not hostnames (confirmed 2026-08-29):**
      sparkrun resolves `layout.placements` / `--hosts` entries against cluster
      host IP addresses — a hostname there is not recognized. Host entries in
      `config.yaml` may set `ip:` (their tailnet IP); the rendered layout pins
      and the `--hosts` flag use that IP (falling back to the name when unset).
- [ ] **Exact sparkrun multi-node CLI:** `setup ssh --hosts`, `cluster create
      <name> --hosts`, `run --cluster`, and the stop/remove equivalents. The
      `spark-lab` converge engine uses these; validate the flags against the
      installed sparkrun version.
- [ ] **Controller selection:** which node runs the CLI/controller? Is it the
      first in `hosts`, or explicit? Does the peer need sparkrun running, or just
      docker?
- [ ] **Image distribution:** does sparkrun pull the model image on each node, or
      must it be pre-pulled (and are the two Sparks on a registry cache)?
- [ ] **Node placement:** how `min_nodes` + `node_assignment` actually place the
      workload (auto-balanced vs pinned).
- [ ] **Networking for the model's inter-node traffic** (tensor/pipeline
      parallel): does sparkrun wire up the GPU interconnect / RDMA, or is plain
      (tailnet) TCP the transport?

These five are the real unknowns; everything else is already handled by the
converge engine. Confirm them during the first live cluster attempt and fold the
answers back into this doc + (if a flag differs) into `sparklab/core/converge.py`.
