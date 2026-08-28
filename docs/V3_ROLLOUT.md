# v3 rollout: one cluster config + model scaling — plan + status

**Status (2026-08-28): COMPLETE.** Phases 1–4 all done and verified live:

- Phases 1–3: feature + tests on `cli-simplify` (commit `94a262b`).
- Phase 4 live migration: done 2026-08-28. Both Sparks run the single cluster
  config from the laptop (`--hosts` remote fan-out) AND node-local (local
  auto-detection verified: `[luna] local` / `[sol] local`). Completions
  verified end-to-end: `LUNA-OK-7` / `SOL-OK-42`.
- Fixes found + shipped during the live migration (all on `cli-simplify`, CI
  green at tip `3742599`):
  - `38ba4fd` test isolation (system-check tests vs repo-root config)
  - `2e5fdd7` explicit file modes on converge writes (0644, .env 0600) —
    luna's prometheus/grafana had been crash-looping on 0600 SFTP files
  - `3742599` removal of files that no longer render (stale-recipe cleanup)
- Remaining: merge `cli-simplify` → `main` (node checkouts sit on the branch
  until then, re-pull `main` after). `labs/` still on disk (user to confirm
  deletion; contents are superseded by the cluster config + merged `.env`).

## Monitoring-role split + example cleanup (2026-08-28, follow-on)

- **`monitoring.role` per host** (commit `70b54c2`, ADR-0008 addendum): `full`
  (default: prometheus+grafana+exporters) / `exporters` (sidecars only — a
  `full` host's prometheus scrapes it over its `ssh` address, tagged with its
  `instance_label`) / `none`. The user's ask — *Grafana on sol, exporters on
  luna, monitor luna from sol* — is now one config line: luna =
  `role: exporters`. Live-verified: sol's prometheus shows `node_luna` /
  `dcgm_luna` / `cadvisor_luna` all `up`; `count(node_uname_info)` = 2.
- **Prometheus hot-reload** (commit `f0b61d9`): a changed bind-mounted
  `prometheus.yml` is invisible to a running daemon (compose up only recreates
  on service-definition change). The plan now appends a best-effort
  `curl -X POST .../-/reload` when a previously-tracked `prometheus.yml`
  changes on a `full` host. (Caught live: sol stayed on the old config after
  the migration until a manual reload.)
- **Live config the user then set:** the single model is `hosts: [sol]` (luna
  scaled down to control-plane + exporters only). Today: **sol** = model +
  full observability stack; **luna** = litellm gateway (no model) + exporter
  sidecars, scraped by sol. Both nodes + the laptop converge cleanly (remote
  and node-local dry-runs: "already converged"); 184 tests green.
- **Config example cleanup** (commit `1bf3504`): only the v3 example remains,
  under its canonical name `config.example.yaml` (so `init` ships v3). The v1
  and v2 examples were removed; refs updated (README/ARCHITECTURE/REGISTRY/
  SPEC).

## Control-plane split + volume invariant (2026-08-28, follow-on)

- **`control_plane.enabled` per host** (commit `86c62a6`, ADR-0008 addendum
  #2): `false` makes a host observability-only — no LiteLLM gateway/DB/Redis
  services, no `litellm/config.yaml`/`model_config.yaml`/`.env` (converge
  removes them; `--remove-orphans` stops the containers). Invariants enforced
  by `check`/`validate` (and `model up`): a model-serving host keeps the
  control plane on; a control-plane-off host must still run monitoring.
  193 tests green; default renders byte-identical (goldens unchanged).
- **Live:** luna is now `control_plane: {enabled: false}` + `role: exporters`
  — it runs just the four exporter sidecars; its gateway/DB/Redis containers
  were stopped. **Volume-destruction invariant** (documented in ARCHITECTURE /
  OPERATIONS): the named volumes are destroyed *only* by `teardown --yes
  --purge` (per-volume warning, DB named unrecoverable). Verified live on
  luna: `docker volume ls` still shows `litellm_postgres_data` +
  `litellm_redis_data` after the gateway went away. Re-enabling + `apply`
  restores the gateway on the previous database.
- **Live layout today:** SOL = model + gateway + full observability; LUNA =
  exporter sidecars only (scraped by sol's prometheus, tagged
  `instance: luna`).

Live-state notes (2026-08-28):
- sol's model had been stopped since ~Aug 17 (no sparkrun container); the
  routine `apply --hosts sol` restored it (ensure, no restart gate needed).
- sol's `.env`-driven DB password differs from luna's → `LITELLM_DB_PASSWORD_SOL`
  (sol's `litellm.db.password_env` override), same for the litellm master/salt
  keys and the grafana admin password (per-host env-var NAMES, one merged
  `.env` on the laptop).
- `install.name` is a per-host override (luna-lab / adam-spark) because it is
  rendered into the recipe `metadata.description` — a shared name would have
  changed sol's recipe hash and forced a needless restart.
- luna's `sparkrun status` display shows 2 hosts (10.0.4.27 = sol via LAN) —
  display quirk from a pre-existing sparkrun multi-host registry; converge
  always scopes with `--hosts 127.0.0.1`, so it's harmless.
- luna's `~/AI/sparkrun/recipes/qwen3-0.6b.yaml` and sol's
  `deepseek-v4-flash-0731.yaml` + `.bak` files are unmanaged user files —
  left alone by converge (only state-tracked files are removed).

---

Original plan (kept for reference):

**Status (2026-08-27):** Phases 1–3 implemented, tested, committed on branch
`cli-simplify` (commit `94a262b`). **Phase 4 (live migration of luna + sol) is
the remaining work** — detailed below. Read this top-to-bottom after a compact;
[ADR-0008](adr/0008-multi-host-cluster-config.md) holds the full design.

## What is done (Phases 1–3)

Commits on `cli-simplify` (not yet pushed):
- `161c33f` feat(cli): model stop (predecessor, kept as legacy `model stop`)
- `94a262b` feat: v3 cluster config — hosts list, --hosts fan-out, model scaling
  (ADR-0008): 172 tests green (was 132), legacy goldens byte-identical + new v3
  golden (`tests/golden/expected_v3_sha256.json`).

Key mechanics (the parts that are easy to forget):

- **Schema v3**: top-level `hosts:` list (`name`/`ssh`/`remote` + *any other key
  = per-host config override*, deep-merged over the cluster-wide document) +
  `models.<m>.hosts` (where served = the scale; absent = all, `[]` = nowhere)
  + `models.<m>.host_overrides.<host>` (per-host model tailoring).
  `host_overrides.<host>.litellm` ALSO applies to the top-level `litellm:`
  gateway section (serving identity) — see `Config.view_for` in
  `sparklab/core/config.py`.
- **`Config.view_for(host)`** = full `Config` with that host's overrides;
  render/converge/plan/state all run per view. `active` selection is
  host-filtered; a host with no serving model converges control-plane only
  (no recipe file). One active model per host is enforced at load
  (`active_host_conflicts`); a fully scaled-down v3 config (no active models
  anywhere) is valid.
- **`core/cluster.py`**: `select_hosts` (config order), local auto-detection
  (`is_on_host`: hostname//etc/hostname/FQDN/primary-IP vs name + ssh host,
  full + first label; `remote: false` unconditionally local), `HostTarget`
  (view + runtime + fs + state), `run_on_each` (sectioned output, continue
  past failure, aggregate rc). Legacy v1/v2 configs = single implicit host,
  byte-identical behavior.
- **Model scaling** (`commands/model.py`): `up` = add hosts + `active: true` +
  converge (routine, no gate); `down --yes` = remove hosts + converge with
  restart gate passed (stale stops); both rewrite `config.yaml` (YAML
  round-trip: **comments not preserved**).
- **`init --hosts`** = idempotent host bootstrap (tools via `check system
  --install` semantics, git checkout from `install.repo_url` — clone if
  missing, ff-only pull if clean, skip if dirty, install_dir, tailscale
  best-effort); report-only without `--yes`.
- **CLI**: `check [--images] [--probe] [--system] [--install] [--all]`
  (positional `config|images|system` legacy form still works); `validate`/
  `doctor` hidden aliases; `apply --restart-model` (replaces `--apply`,
  `--apply`/`--yes` still work as hidden aliases); `upgrade --yes` gated;
  `logs` needs exactly one host.
- **State unchanged**: one `state.json` per node (node-side
  `<repo_dir>/.sparklab-state/state.json`, remote `RemoteState`; local checkout
  for the local host).

## Phase 4 — live migration (NOT YET DONE)

Goal: replace the per-node `labs/{luna,sol}/` configs with ONE cluster config
on the laptop (and later on each node), using the new tooling end to end.
This also applies the **pending luna tuned-recipe change** (first run downloads
the DSpark draft model; model restarts; this session's gateway is luna's, so
plan for a drop).

### 4.0 Push the branch first

```bash
cd ~/Development/AdamFerguson/spark-lab
git push -u origin cli-simplify     # CI runs; both commits are local-only today
```

### 4.1 Author the cluster config (on the laptop)

File: `~/Development/AdamFerguson/spark-lab/config.yaml` (gitignored; no
secrets in it — all secret values live in `.env`).

Source of truth for values: `labs/luna/config.yaml` + `labs/sol/config.yaml`
(their node-side counterparts). The draft shape (fill/verify every value from
those two files):

```yaml
version: 3
install:
  name: spark-lab
  install_dir: ~/AI
  repo_dir: ~/spark-lab
  # repo_url: <git URL>            # optional; enables init --hosts cloning
hosts:
  - name: luna
    ssh: luna                    # tailnet name (resolves from pop-os; also on the nodes)
    remote: true
    monitoring:
      instance_label: luna
      dashboards: [sglang-dashboard]
    images:
      litellm: docker.litellm.ai/berriai/litellm:main-stable
  - name: sol
    ssh: sol
    remote: true
    monitoring:
      instance_label: adam-spark
      dashboards: [sglang-dashboard, spark-host-overview]
    images:
      litellm: ghcr.io/berriai/litellm:v1.99.0-dev.2
    litellm:                     # per-host secret ENV-VAR NAMES (values in .env)
      master_key_env: LITELLM_MASTER_KEY_SOL
      salt_key_env: LITELLM_SALT_KEY_SOL
models:
  qwen-3-8-27b-dspark-nvfp4:     # == sol's CURRENT recipe name (see 4.3 rationale)
    active: true
    hosts: [luna, sol]
    runtime: sglang
    hf_model: RadixArk/Qwen3.8-27B-NVFP4
    image: lmsysorg/sglang:qwen38-27b
    hf_token_env: HF_TOKEN
    host: 0.0.0.0
    port: 30000
    min_nodes: 1
    resources:
      mem_fraction_static: 0.85
      node_assignment: auto
    params:                      # the SHARED tuned set (luna's current config == sol's)
      kv_cache_dtype: fp8_e4m3
      attention_backend: flashinfer
      chunked_prefill_size: 2048
      max_running_requests: 12
      reasoning_parser: qwen3
      tool_call_parser: qwen3_coder
      mamba_full_memory_ratio: 0.5
      max_mamba_cache_size: 48
      mamba_radix_cache_strategy: extra_buffer_lazy
      mamba_ssm_dtype: bfloat16
      speculative_algorithm: DSPARK
      speculative_draft_model: RadixArk/Qwen3.8-27B-DSpark
      speculative_draft_attention_backend: flashinfer
    flag_map:
      speculative_draft_model: speculative-draft-model-path
    extra_flags:
      - --enable-metrics
      - --enable-mfu-metrics
      - --enable-cache-report
      - --trust-remote-code
      - --disable-prefill-cuda-graph
    host_overrides:
      luna:
        litellm:
          model_name: Qwen3.8-27B-NVFP4
          model_info:
            supports_vision: true
            max_input_tokens: 32768
            max_output_tokens: 8192
      sol:
        litellm:
          model_name: adam-spark-qwen3-8-27b
          model_info:
            supports_vision: true
            max_input_tokens: 262144
            max_output_tokens: 131072
            input_cost_per_token: 0.00000045
            output_cost_per_token: 0.000000320
            cache_read_input_token_cost: 0.000000045
            supports_reasoning: true
            supported_reasoning_efforts: [low, medium, xhigh]
active_models:
  - qwen-3-8-27b-dspark-nvfp4
images:
  db: pgvector/pgvector:pg16
  redis: redis:7-alpine
  prometheus: prom/prometheus
  grafana: grafana/grafana
  node_exporter: prom/node-exporter:latest
  dcgm_exporter: utkuozdemir/nvidia_gpu_exporter:latest
  cadvisor: gcr.io/cadvisor/cadvisor:latest
  gpu_textfile: ubuntu:24.04
litellm:
  model_name: qwen-3-8-27b-dspark-nvfp4   # default; host_overrides win per host
  port: 4000
  model_api_base_host: host.docker.internal
  master_key_env: LITELLM_MASTER_KEY
  salt_key_env: LITELLM_SALT_KEY
  db:
    user: litellm
    password_env: LITELLM_DB_PASSWORD
    db: litellm
  redis:
    enabled: true
    port: 6379
monitoring:
  enabled: true
  prometheus: {port: 9090, retention: 15d}
  grafana: {port: 3000, admin_password_env: GRAFANA_ADMIN_PASSWORD}
network:
  tailscale: {enabled: true}
  cloudflare: {enabled: false}
```

(Verify against the two live configs — especially sol's `model_info` cost
numbers and whether `LITELLM_DB_PASSWORD`/`GRAFANA_ADMIN_PASSWORD` are the same
on both nodes; if GRAFANA differs, give sol a
`GRAFANA_ADMIN_PASSWORD_SOL` env name via the same override mechanism.)

### 4.2 Merge the `.env` (node-side value handling; no secrets in chat)

One `.env` next to the cluster config. Values differ per node for
`LITELLM_MASTER_KEY`/`LITELLM_SALT_KEY` (two gateways). Procedure (do it with
shell only; never print values):

```bash
cd ~/Development/AdamFerguson/spark-lab
ssh adam@luna "grep -E '^(LITELLM_MASTER_KEY|LITELLM_SALT_KEY|LITELLM_DB_PASSWORD|GRAFANA_ADMIN_PASSWORD|HF_TOKEN)=' ~/spark-lab/.env" > /tmp/cluster.env
ssh adam@sol  "grep -E '^(LITELLM_MASTER_KEY|LITELLM_SALT_KEY)=' ~/spark-lab/.env | sed 's/^LITELLM_MASTER_KEY=/LITELLM_MASTER_KEY_SOL=/; s/^LITELLM_SALT_KEY=/LITELLM_SALT_KEY_SOL=/'" >> /tmp/cluster.env
HF_TOKEN=$(ssh adam@luna "grep '^HF_TOKEN=' ~/spark-lab/.env"); echo "HF_TOKEN=$HF_TOKEN" >> /tmp/cluster.env
mv /tmp/cluster.env .env && chmod 600 .env
# sanity: no value lines may be missing/duplicated
```
(Gotcha remembered: sol's litellm .env values were historically quoted — the
node `~/spark-lab/.env` files are the clean source; strip any surrounding
quotes if present.)

### 4.3 Why the model is named `qwen-3-8-27b-dspark-nvfp4`

- It equals **sol's current on-disk recipe name AND content** → sol's model
  entry in state matches the render → **sol converges with NO restart**
  (idempotent, zero downtime for sol).
- Luna's recipe is named `Qwen3.8-27B-NVFP4` and is drifted (the tuned
  params are in luna's node config but not yet applied) → luna converges as:
  stop old recipe (gated) + write new recipe + detached start + bounded
  probe — i.e. exactly the pending `apply --apply` change, done via the new
  system.
- Verify both predictions with `apply --dry-run` BEFORE the real run:
  expect sol = "(none -- already converged)", luna = changed recipe +
  stop/start.

### 4.4 Verify from the laptop (safe steps first)

```bash
bin/spark-lab check --hosts luna,sol           # per-host render + node binaries
bin/spark-lab status --hosts luna,sol          # live stacks read back remotely
bin/spark-lab apply --dry-run --hosts luna,sol # expect: sol converged, luna 1 change
```
If sol is NOT "already converged": diff the rendered sol recipe vs the node's
on-disk one (`apply --dry-run --diff --hosts sol`) and fix the cluster config
until sol is a clean no-op. Do NOT proceed to 4.5 with a sol drift.

### 4.5 Apply the pending change on luna (model restart)

**This session runs through luna's gateway** (`luna:4000`) — the model restart
will drop it mid-conversation. Run it detached so the turn survives:

```bash
cd ~/Development/AdamFerguson/spark-lab
nohup bin/spark-lab apply --restart-model --hosts luna > /tmp/luna-v3-apply.log 2>&1 &
# then, later turns: tail /tmp/luna-v3-apply.log
```
Expect: stop old model → write tuned recipe (`qwen-3-8-27b-dspark-nvfp4.yaml`)
→ detached start → bounded probe (up to ~10 min; first run downloads the
DSpark draft model). Afterwards:
- `bin/spark-lab status --hosts luna` (or `ssh adam@luna 'sparkrun status'`)
- gateway check: `curl -s localhost... via tailnet` → `http://luna.tail9d5411.ts.net:4000/health/liveliness`
- a completion through the gateway (SOL-OK-42 style probe)
- check for the pre-existing orphan: luna had TWO model containers before;
  `sparkrun status` on luna should show only one managed job — if an orphan
  container lingers, `docker rm` it manually (out of scope for the converge).
- routine re-check: `bin/spark-lab apply --dry-run --hosts luna,sol` → both
  converged.

### 4.6 Node-local: same cluster config on each Spark (operator case 3)

```bash
for h in luna sol; do
  ssh adam@$h 'cp ~/spark-lab/config.yaml ~/spark-lab/config.yaml.bak-v2-20260827; cp ~/spark-lab/.env ~/spark-lab/.env.bak-v2-20260827'
done
scp .env ~/spark-lab/  # per node: cluster .env replaces node .env (superset)
scp config.yaml ...    # same for config
```
Then verify auto-detection on the node:
```bash
ssh adam@luna 'cd ~/spark-lab && bin/spark-lab status --hosts luna'   # must say [luna] local (no SSH to itself)
ssh adam@luna 'cd ~/spark-lab && bin/spark-lab apply --dry-run --hosts luna'
```
(Cross-node: `apply` from luna for BOTH hosts needs luna→sol ssh keys;
self-targeting needs nothing. The optional `init --mesh` follow-up in
ADR-0008 covers the key exchange.)

### 4.7 Retire `labs/`

After 4.5–4.6 are green and the user confirms:
```bash
rm -rf labs/        # contents now live in the cluster config + single .env
```
(`labs/` is gitignored; nothing committed references it after this phase.)

### 4.8 Merge

Push `cli-simplify`, CI green → merge to `main` (user's call). Update
scratchpad: close the "README update" + "CLI simplification (deferred)"
items (both substantially addressed by `94a262b`).

## Gotchas / invariants to preserve

- **`hosts: []` ≠ missing `hosts:`** (empty = scaled down nowhere; missing =
  all hosts). Don't "simplify" the falsy checks.
- `apply` on a host with no active model must NOT fail the image fail-safe
  (it's gated on `cfg.model` in `apply._converge_one`).
- The per-host view's active-model litellm override is applied to the gateway
  section in `view_for` (serving identity) — a regression here = wrong
  model_name rendered on one host.
- `migrate` output keeps working configs; `model up/down` YAML round-trip
  drops comments (documented).
- Remote commands: `bash -lc` + `~/.local/bin` prefix; fabric memoizes the
  SFTP client (never close it); data probes run under
  `contextlib.redirect_stdout`.
- Secrets: per-host env-var NAMES via `hosts[].litellm.master_key_env`
  overrides; values stay in the single `.env` (chmod 600, gitignored).
- Luna known-state: possibly two model containers pre-migration (one orphan).
- `config.yaml`/`.env` at the spark-lab repo root are gitignored operator
  files (a stale v1 "heliosphere" `config.yaml` exists at the repo root —
  that is what the cluster config replaces).

## Open follow-ups (deliberately out of scope)

- `init --mesh`: node-to-node ssh key exchange via the operator's connections.
- Multi-Spark TP placement (ADR-0007; `min_nodes > 1` currently rejected).
- `recipes` as an agent-driven discovery/optimization tool (user's later
  project; current `recipes` command untouched).
