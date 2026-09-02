# Architecture

How the pieces fit together, and why each one exists.

## Data flow

```
                         ┌─────────────────────────────────────────────┐
   your client ──► :4000 │  LiteLLM gateway        Postgres  Redis     │
   (OpenAI SDK)          │  (keys, spend, routing) (models/  (cache +  │
                         │                   logs)    routing)        │
                         └───────────────┬───────────────────────────┘
                                         │ OpenAI-compatible :30000
                                         ▼
                         ┌─────────────────────────────────────────────┐
                         │  SGLang  (served by sparkrun, in a Docker    │
                         │  container on the host; GPU via nvidia)      │
                         └─────────────────────────────────────────────┘

   Prometheus :9090 ── scrapes ── SGLang :30000/metrics, node_exporter,
                                  DCGM :9835, cAdvisor :8080, + a
                                  nvidia-smi text-file sidecar
        │
        ▼
   Grafana :3000 ── dashboards (SGLang, host overview)

   Access:  Tailscale (private mesh)   ·   Cloudflare Tunnel (optional public)
```

## Why each piece

- **sparkrun** is the critical dependency + workload orchestrator. It launches
  the model as a Docker container with the right flags for GB10 and knows how
  to run it on one node or fan out across a cluster via a passwordless SSH
  mesh. `spark-lab` drives it; you rarely call it directly. The inference
  engine is **whatever the recipe declares** (the recipe's `runtime:` + serve
  command) -- SGLang is the default, not a requirement.
- **The inference engine (SGLang by default)** is the actual model server. The
  default SGLang build exposes an OpenAI-compatible endpoint on `:30000` and,
  crucially, a Prometheus `/metrics` endpoint (enabled by `--enable-metrics`)
  that the dashboards read. Any engine that serves an OpenAI-compatible API +
  exposes metrics fits the same shape (see the `runtime:` / `serve_command`
  config fields for non-SGLang recipes).
- **LiteLLM** sits in front as the thing you actually talk to. You don't point
  clients at the raw engine port — you point them at LiteLLM (`:4000`), which
  gives you API keys, per-key spend tracking (stored in Postgres), caching
  (Redis), and a stable model name that survives model swaps.
- **Postgres** (with `pgvector`) stores LiteLLM's models, virtual keys, and
  spend logs. **Redis** backs response caching and multi-instance routing.
- **Prometheus + Grafana** turn the raw metrics into something you can read.
  The SGLang build exports metric names with a colon (`sglang:foo`); the
  Prometheus job rewrites them to underscores (`sglang_foo`) so the dashboards'
  queries match. See the `metric_relabel_configs` in the Prometheus config.
- **GPU observability on a GB10** is fiddly. The Spark's GPU shares unified
  memory with the host, so "VRAM" is really host memory. The stack therefore:
  - disables the flaky `hwmon`/`cpufreq` node-exporter collectors (they hang
    scrapes on the GB10),
  - runs a **DCGM exporter** for clocks/temps/power/utilization,
  - runs a small **`nvidia-smi` text-file sidecar** that publishes
    `nvidia_gpu_*` gauges node-exporter can serve, and
  - runs **cAdvisor** for per-container CPU/RAM.
- **Tailscale** makes the gateway reachable from any of your devices over a
  private mesh — no open ports. **Cloudflare Tunnel** (optional) is how you
  safely expose it publicly to specific people via a Cloudflare token.

## The converge model

`spark-lab apply` is declarative:

1. **Render** templates → a `deploy/` tree from `config.yaml` + `.env`.
2. **Diff** each rendered file's SHA-256 against `.sparklab-state/state.json`.
3. **Act only on the difference**: bring the LiteLLM + monitoring **control
   plane up first** (if its files changed) so the gateway + observability are
   available while the model loads; then **launch the model detached** (only if
   the recipe is new or changed *and* you opt in with `--apply`), and **probe
   the model's `/health` with a bounded wait** instead of blocking on its log
   tail -- so a failed model start is reported rather than hanging the converge;
   finally (re)enable the network services.
4. **Record** the new hashes, so the next run is a no-op unless something
   changed.

This is why "edit the config, run `apply`" reliably brings the node to the new
state without manually restarting the right services.

## Config schema (v3)

`config.yaml` must declare `version: 3` (older shapes are retired):

- **`hosts:`** — the managed nodes: `name` / `ssh` / `remote` / `ip` plus any
  per-host config override, deep-merged over the cluster-wide document. The
  engine converges per `Config.view_for(host)` (a full config with that
  host's overrides applied).
- **`models:`** — a keyed map from *alias* → model definition: either an
  inline launch spec or a `recipe:` reference (the source of truth, below).
  `active:` participates in placement + validation; exactly one active model
  per host.
- **Per-model hosting (ADR 0008):**
  `models.<m>.hosts` (where it is served — the scale) and
  `models.<m>.host_overrides.<host>` (per-host model tailoring, including its
  litellm serving identity). `Config.view_for(host)` returns a full config with
  that host's overrides applied; the engine converges per view. Local
  auto-detection makes the same file work on
  every Spark (its own entry converges locally, the rest over SSH).
  `monitoring.role` (per host) splits the observability stack: `full`
  (default: prometheus + grafana + exporters) / `exporters` (sidecars only —
  a `full` host's prometheus scrapes it over its `ssh` address, tagged with
  its `instance_label`) / `none`. `control_plane.enabled` (per host, default
  `true`) splits the LiteLLM control plane: `false` makes a host
  observability-only (no gateway/DB/Redis, no litellm config files).
  **Run vs serve:** `models.<m>.hosts` is where the model RUNS; serving is
  implicit — every active model with a running host is registered in the
  `model_list` of every control-plane host, with `api_base` pointing at the
  local engine when the model runs on that host and at the running host's
  tailnet/LAN address otherwise (so a model on luna is served by sol's
  gateway). Invariants: a control-plane-off host must still run monitoring;
  active models need ≥1 control-plane host to serve them; distinct serving
  names per gateway. See ADR 0008's addenda.
- **v3 — recipes as source of truth (ADR 0009).** A model block may reference
  a recipe instead of declaring the launch inline: `models.<m>.recipe: <name>`
  resolves `<config dir>/recipes/<name>.yaml` (a **plain sparkrun recipe** —
  only sparkrun-known keys, directly `sparkrun run`-able, no secrets or
  layout) and folds its launch spec into the block (inline keys win; the
  inline form stays supported as the documented legacy). Two spark-lab
  extensions live in the recipe's documented free-form `metadata:` section:
  `metadata.litellm:` (gateway name + `model_info`) and
  `metadata.readiness_seconds:` (the probe bound). Placement is structural:
  the RENDERED recipe gains a `layout:` pin from `hosts:` (each scheduler
  honors an explicit layout verbatim; `--ensure` matching is intent-based and
  layout-independent, so the pin cannot defeat it) and the `HF_TOKEN` env
  value (from `models.<m>.hf_token_env` + `.env`); repo recipes stay clean.
  `spark-lab status` prints the derived host → model placement table.

`spark-lab migrate` rewrites a v1/v2 file to v3 on disk (idempotent,
value-preserving, chained through `upgrade_to_v2` + `upgrade_to_v3`); the
compat loader makes the on-disk format optional.
See `config.example.yaml` (v3) for the full document.

## Commands

Every host-targeted command takes `--hosts a,b` (v3; unset = all hosts).

| Command | Purpose |
|---|---|
| `init [--yes]` | create `config.yaml` + `.env`, generate placeholder keys |
| `init --hosts a,b [--yes] [--all]` | idempotent host bootstrap: tools, git checkout, install dir, tailscale |
| `apply [--dry-run] [--diff] [--restart-model]` | render + converge per host; `--diff` shows what would change on disk |
| `model up <m> [--hosts x]` | scale a model up: add host(s) to `models.<m>.hosts` + converge |
| `model down <m> --yes [--hosts x]` | scale a model down: remove host(s) + stop the workloads there |
| `model stop --yes` | stop the model workload now (config unchanged; next apply restarts) |
| `adopt [--dry-run]` | take over an existing running install: record on-disk state + the running model; read-only vs the install, no restart |
| `check [--images] [--probe] [--system] [--install] [--all]` | pre-flight: config render + binaries; image resolution; per-host system tools |
| `migrate [--dry-run]` | rewrite a v1/v2 config to schema v3 (idempotent) |
| `logs <service> [--lines N] [-f]` | tail stack service logs (one host; `--hosts` to pick) |
| `status` | workloads + stack + network status, per selected host |
| `teardown [--yes] [--purge]` | stop the model + remove the stack, per selected host |
| `upgrade --yes` | refresh engine deps + sparkrun + images, then re-apply, per host |
| `validate` / `doctor` | hidden aliases: `check` / `check --system` |

`apply` is fail-safe: it refuses to converge when the active model has no
resolvable image, and a host serving no model converges
control-plane only.

Two file-layer invariants (both learned live, 2026-08-28):

- **Scaled-down / renamed recipes are KEPT on disk, unmanaged.** A recipe that
  no longer renders (the model scaled off the host, or a rename) is dropped
  from the state record but *left in place*: with the workload stopped the
  file is inert — the ensure/stop paths always address an explicit path, and
  nothing in spark-lab scans the recipes directory — and re-scaling up just
  re-renders over it. (Earlier versions deleted it; the deletion bought
  nothing but a moving part.) All other managed files (gateway/monitoring
  configs) are still deleted, with two safety gates: only after the apply's
  commands succeeded, and deferred entirely while the model restart is still
  gated.
- **Explicit file modes on every write.** SFTP-created files land 0600 and
  local writes honor the process umask (077 on the Sparks); both are unreadable
  by the container users of prometheus/grafana, which crash-loop on 0600
  config files. Converge therefore sets the mode explicitly: rendered
  `litellm/.env` (secrets) 0600, everything else 0644. Files written *before*
  this fix are not re-written by an unchanged apply, so a node that shows
  `permission denied` in its prometheus/grafana logs needs a one-time manual
  `chmod 644` on the install-dir config files (see OPERATIONS).
- **Named volumes are destroyed only by `teardown --yes --purge`** (the
  single `docker compose down -v` in the codebase). No other command —
  apply/converge reconcile, `model up/down/stop`, disabling
  `control_plane` — deletes `litellm_postgres_data` (the LiteLLM database),
  `litellm_redis_data`, `litellm_prometheus_data` or
  `litellm_grafana_data`. Disabling the control plane on a host stops its
  gateway/db/redis containers (`--remove-orphans`) and removes their config
  files, but the named volumes remain on disk, so re-enabling + `apply`
  restores the stack on the previous data.
- **Changed gateway files hot-swap the running daemons.** A bind-mounted
  config change is invisible to a running service whose compose definition
  didn't change: when a previously-tracked `litellm/prometheus.yml` changes
  the plan hot-reloads prometheus (`POST /-/reload`), and when a tracked
  `litellm/model_config.yaml` or `litellm/config.yaml` changes it
  best-effort `docker compose restart litellm` — a running gateway keeps its
  boot-time model list, so entry flips (add/remove, local → remote `api_base`)
  would otherwise sit inert until a manual restart. Both are best-effort: a
  fresh stack already booted from the new files.
