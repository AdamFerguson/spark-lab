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
  config fields + the registry path for non-SGLang recipes).
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
- **v3 — one config per cluster (ADR 0008).** `version: 3` adds a `hosts:`
  list (managed nodes: `name` / `ssh` / `remote` + any per-host config override,
  deep-merged over the cluster-wide document) and per-model hosting:
  `models.<m>.hosts` (where it is served — the scale) and
  `models.<m>.host_overrides.<host>` (per-host model tailoring, including its
  litellm serving identity). `Config.view_for(host)` returns a full config with
  that host's overrides applied; the engine converges per view, so v1/v2 keep
  byte-identical behavior. Local auto-detection makes the same file work on
  every Spark (its own entry converges locally, the rest over SSH).

`spark-lab migrate` rewrites a v1/v2 file to v3 on disk (idempotent,
value-preserving, chained through `upgrade_to_v2` + `upgrade_to_v3`); the
compat loader makes the on-disk format optional.
See `config.example.v2.yaml` and `config.example.v3.yaml` for full documents.

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
| `recipes search <q>` | fan a query out to enabled discovery sources, merge + dedup |
| `recipes list [src]` | enumerate one source (or all) |
| `recipes show <ref>` | resolve `<source://ref>`, print metadata + native body |
| `recipes convert <ref>` | produce a sparkrun *candidate* recipe (validated, never applied) |
| `logs <service> [--lines N] [-f]` | tail stack service logs (one host; `--hosts` to pick) |
| `status` | workloads + stack + network status, per selected host |
| `teardown [--yes] [--purge]` | stop the model + remove the stack, per selected host |
| `upgrade --yes` | refresh engine deps + sparkrun + images, then re-apply, per host |
| `validate` / `doctor` | hidden aliases: `check` / `check --system` |

`apply` is fail-safe: it refuses to converge when the active model has no
resolvable image (ADR 0004), and a host serving no model converges
control-plane only.

Two file-layer invariants (both learned live, 2026-08-28):

- **Removed files are deleted, with two safety gates.** State-tracked files that
  no longer render (an old recipe after a rename) are deleted only after the
  apply's commands succeeded — the stop-model command addresses the old recipe
  by its on-disk path — and are deferred entirely while the model restart is
  still gated (the stale path must survive until the post-restart apply).
- **Explicit file modes on every write.** SFTP-created files land 0600 and
  local writes honor the process umask (077 on the Sparks); both are unreadable
  by the container users of prometheus/grafana, which crash-loop on 0600
  config files. Converge therefore sets the mode explicitly: rendered
  `litellm/.env` (secrets) 0600, everything else 0644. Files written *before*
  this fix are not re-written by an unchanged apply, so a node that shows
  `permission denied` in its prometheus/grafana logs needs a one-time manual
  `chmod 644` on the install-dir config files (see OPERATIONS).

## Recipe discovery + auto-conversion (Phase 5, ADR 0003)

Discovery is **plugin-based and config-driven**. A `RecipeSource` emits
source-agnostic `DiscoveredRecipe` records; the framework (registry + contract +
record type) lives in `sparklab/core/discovery/`, and which sources exist comes
entirely from the `discovery:` section of `config.yaml`. Two built-in adapters
ship today:

- **`sparkrun-registry`** — a registry of ready-to-run sparkrun recipes. Default
  is the in-repo one: `.sparkrun/registry.yaml` (index) + `recipes/*.yaml`.
- **`sglang-cookbook`** — a curated collection of SGLang model entries (not
  sparkrun documents). Default sample: `cookbook/sglang.sample.json`.

Adding/redirecting a source is a config change; a brand-new *kind* is a package
installed under the `sparklab.recipe_sources` entry point + a config entry. Each
source is read-only, non-disruptive, and errors are isolated per-source. `recipes
convert` turns a discovered record into a sparkrun **candidate** (deterministic
normalization, with an opt-in LLM-assisted refinement that falls back to
deterministic on any failure) -- always validated, written to a file the user
reviews, and **never auto-applied**. See `docs/REGISTRY.md`.
