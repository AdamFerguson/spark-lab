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

- **sparkrun** is the workload orchestrator. It launches the model as a Docker
  container with the right flags for GB10 (see [MODEL_RECIPES](MODEL_RECIPES.md))
  and knows how to run it on one node or fan out across a cluster via a
  passwordless SSH mesh. `spark-lab` drives it; you rarely call it directly.
- **SGLang** is the actual model server. It exposes an OpenAI-compatible
  endpoint on `:30000` and, crucially, a Prometheus `/metrics` endpoint
  (enabled by `--enable-metrics`) that the dashboards read.
- **LiteLLM** sits in front as the thing you actually talk to. You don't point
  clients at the raw SGLang port — you point them at LiteLLM (`:4000`), which
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
3. **Act only on the difference**: recreate the LiteLLM stack if its files
   changed; restart the model only if the recipe changed *and* you opt in with
   `--apply`; (re)enable the network services.
4. **Record** the new hashes, so the next run is a no-op unless something
   changed.

This is why "edit the config, run `apply`" reliably brings the node to the new
state without manually restarting the right services.

## Config schema (v2, ADR 0004)

`config.yaml` is versioned and strictly **additive**. A file with no `version:`
key is **v1** (a single `model:` block) and still works; it renders
byte-identically. **v2** adds multi-model, an explicit image map, and profiles:

- **`models:`** — a keyed map from *alias* → model definition. Each def is the
  v1 `model:` block plus `active:` (which one is live under sparkrun) and
  `resources:` (allocation: `mem_fraction_static`, `node_assignment`,
  `priority`, `concurrency`). A top-level `active_models:` list names the live
  alias and wins over the per-model `active:` flags. Exactly one model is
  active on a single node; switching it + `apply` is the gated model-restart
  path.
- **`images:`** — every container image declared in config. Resolution
  precedence (high → low): env `SPARKLAB_IMAGE_<KEY>` → active `profile:`
  override → the `images:` map → the v1 per-service field → the historical
  default. Model images stay on each model def (`models.<alias>.image`).
- **`profile:` / `profiles:`** — e.g. `profile: dev` selects a `profiles.dev`
  override block (dev/test vs prod). The active model's memory ceiling comes
  from `resources.mem_fraction_static` when present, else `params.mem_fraction_static`.

`spark-lab migrate` rewrites a v1 file to v2 on disk (idempotent, value-
preserving); the compat loader makes the on-disk format optional.
See `config.example.v2.yaml` for a full document.

## Commands

| Command | Purpose |
|---|---|
| `init` | create `config.yaml` + `.env`, generate placeholder keys |
| `apply [--dry-run] [--diff] [--apply]` | render + converge; `--diff` shows what would change on disk |
| `validate` (=`check config`) | read-only pre-flight: schema + render + required binaries |
| `check images [--probe]` | resolve + report every image the deploy will pull; `--probe` inspects manifests |
| `migrate [--dry-run]` | rewrite a v1 config to schema v2 (idempotent) |
| `logs <service> [--lines N] [-f]` | tail stack service logs |
| `status` | workloads + stack + network status |
| `teardown [--yes] [--purge]` | stop the model + remove the stack |
| `upgrade` | refresh engine deps + sparkrun + images, then re-apply |

`apply` is fail-safe: it refuses to converge when the active model has no
resolvable image (ADR 0004).
