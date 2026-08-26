# ADR 0004 — Config schema v2: multi-model, image map, recipes, discovery, and conversion

Status: proposed

## Context

Today `config.yaml` is implicitly **v1**: a single `model:` block plus `litellm:`, `monitoring:`, `network:`, and `install:`. That single-model shape is what `lib/config.py` loads and what `lib/render.py` + `lib/converge.py` consume. The migration plan (`docs/MIGRATION_PLAN.md`) requires, in Phase 4 and Phase 5:

- **Multiple model definitions** with independent resource allocation, and a way to say which model is live under sparkrun at a time (Objective 4).
- **A complete, overridable image map** — every container image resolvable from config, with a dev/test-vs-prod override and an availability check that gates config acceptance (Objective 8).
- **Standalone in-repo `recipes/`** that coexist with the rendered-into-`install_dir` path (Objective 7).
- **Config-driven discovery sources** (ADR 0003) and an **opt-in LLM-assisted conversion** flag + endpoint (Objective 6).

These are all *config-shape* decisions, so they belong in one schema ADR. The engine split that hosts this schema was fixed by ADR 0001 (`sparklab/core/schema.py` holds the declared shape; `config.py` loads; `secrets.py` resolves `.env`), and the discovery section's semantics were fixed by ADR 0003 — this ADR does not re-decide those; it defines the config document they read.

Two hard constraints shape everything here:

- **Backwards compatibility (risk R3):** a v1 `config.yaml` (single `model:`, no `version:`) must load and render **byte-identically** to pre-branch output. The v2 schema is additive; the compat loader upgrades in memory with zero diff.
- **No secrets in config:** every credential is referenced by env-var *name* only (`*_env` keys); values live in `.env` (the invariant already held by `hf_token_env`, `master_key_env`, `admin_password_env`, etc.).

## Decision

Introduce a **versioned, additive schema** with a top-level `version: 2` key. Its absence means v1. A v2 `config.yaml` is a superset of v1: every v1 field keeps its name, location, and meaning; new capability lives in new keys with declared defaults so an omitted key is a no-op.

### The `model:` block becomes `models:`

The single `model:` map becomes `models:`, a **keyed map** from a user-chosen **alias** to a model definition. Each definition is the v1 `model:` block (unchanged fields: `hf_model`, `image`, `hf_token_env`, `host`, `port`, `min_nodes`, `params`, `flag_map`, `extra_flags`) plus two additions:

- **`active: true|false`** (default `false`) — which model is **live under sparkrun at a time**. On a single DGX Spark (one GPU pool) exactly **one** model is active; the others are stored definitions you switch to. Validation: at most one active for a single-node deploy; `apply` deploys the active model, and switching the active model + re-applying is the documented model-restart path (still gated behind the explicit apply flag per the converge invariant).
- **`resources:`** — **independent resource allocation**, distinct from SGLang serve flags:
  - `mem_fraction_static` — the canonical per-model GPU-memory ceiling. (A v1 config carries this inside `params`; the renderer honors `resources.mem_fraction_static` when present and falls back to `params` otherwise, so v1 is unaffected.)
  - `node_assignment` — `auto` (default) or an explicit list of node names; for clusters this pins a model to particular hosts.
  - `priority` — `low|normal|high` (default `normal`); consumed by active-set/scheduling logic, not passed to SGLang.
  - `concurrency` — optional soft cap on in-flight requests for this model, independent of `params.max_running_requests`.

An optional top-level **`active_models:`** list is a convenience override that names the active alias(es) without editing each model's `active` flag; when present it wins. (For a single node the list holds one alias.)

```yaml
version: 2
models:
  qwen38-27b:                  # alias — used in logs, status, and model selection
    active: true
    hf_model: RadixArk/Qwen3.8-27B-NVFP4
    image: lmsysorg/sglang:qwen38-27b
    hf_token_env: HF_TOKEN
    host: 0.0.0.0
    port: 30000
    min_nodes: 1
    resources:
      mem_fraction_static: 0.85
      node_assignment: auto
      priority: normal
    params:                    # SGLang serve flags — unchanged semantics from v1
      kv_cache_dtype: fp8_e4m3
      attention_backend: flashinfer
      # ... (same keys as today's model.params)
    flag_map:
      speculative_draft_model: speculative-draft-model-path
    extra_flags: [ --enable-metrics ]
  llama31-8b:                  # a second, stored model — not active
    active: false
    hf_model: <hf-id>
    image: lmsysorg/sglang:<tag>
    port: 30010
    resources: { mem_fraction_static: 0.60 }
    params: { kv_cache_dtype: fp8_e4m3 }
```

### The image map: every image in config, overridable, and validated

All container images are declared in config (none hardcoded in code). The **shared** stack images are grouped in a top-level `images:` map so the set is explicit and diff-able; **model** images stay on each model definition (`models.<alias>.image`) because they are per-model:

```yaml
images:
  litellm: ghcr.io/berriai/litellm:latest      # the gateway image (newly explicit)
  pgvector: pgvector/pgvector:pg16
  redis: redis:7-alpine
  prometheus: prom/prometheus
  grafana: grafana/grafana
```

Precedence for resolving any image reference, highest to lowest:

1. **Env override** — `SPARKLAB_IMAGE_<KEY>` (e.g. `SPARKLAB_IMAGE_GRAFANA`) targets one service; ideal for testing a single image without touching config.
2. **Profile override** — the active `profile:` (below) may set `images.<key>`.
3. **Explicit value** — `models.<alias>.image` or `images.<key>`.

**Dev/test vs prod** is a **profile**: `profile: prod|dev` (default `prod`) selects a `profiles:` block of overrides:

```yaml
profile: prod
profiles:
  dev:
    images:
      grafana: grafana/grafana:latest
      litellm: ghcr.io/berriai/litellm:latest
```

**Availability validation** is a gate, not a silent assumption: `spark-lab check images` (Phase 4) resolves every image for the active model + enabled stack, probes each reference (manifest / `pull` dry-run) through the runtime seam (ADR 0002), and reports missing/unresolvable images **before** a config change is accepted. `apply` runs the same check and refuses to converge on an unresolvable image — fail-safe, consistent with the gated-restart invariant.

### `recipes/` — standalone, version-controlled, coexisting

A `recipes/` directory at the repo root holds **standalone** sparkrun recipe YAMLs, each independently runnable via `sparkrun` and checked into the repo (Objective 7). It coexists with the rendered path:

- **Rendered path (unchanged):** a `models:` entry is still rendered into `<install_dir>/sparkrun/recipes/<alias>.yaml` at apply time — the v1 behavior, byte-identical for v1 configs.
- **Standalone path (new):** a file under `recipes/` is a first-class, versioned artifact. `apply` may target a named recipe from `recipes/` instead of (or in addition to) rendering a `models:` entry; `spark-lab recipes` (ADR 0003) lists/inspects them, and a discovered/converted recipe (Phase 5) lands here as a **candidate** a human promotes.

The `recipes/` layout and the discovery record contract are ADR 0003's territory; this ADR only fixes *where* they live in config and on disk and the coexistence rule: both paths are available, the active `models:` entry drives the live deploy, and `recipes/` is the durable, shareable store.

### Discovery and conversion: declared in config

- **Discovery** is declared under `discovery:` exactly as ADR 0003 specifies (enabled built-in sources by kind + alias, per-source options, credentials by env-var name only). This ADR does not redefine it.
- **Conversion** is opt-in, and its endpoint is config, not code:

```yaml
conversion:
  enabled: false                 # opt-in; default OFF
  output_dir: recipes/           # candidates land here; never auto-applied
  llm:
    endpoint_env: CONVERSION_LLM_ENDPOINT   # e.g. http://localhost:4000/v1
    model: my-spark-model
    api_key_env: LITELLM_MASTER_KEY          # name, not value
```

The default endpoint is the **local LiteLLM gateway** already on the Spark (assumption A3); a remote endpoint is the same shape (point the env var at another URL). On LLM unreachable / timeout / malformed, conversion degrades to a **manual** fallback (emit the source record + a starter template) and never blocks — the failure-mode rule from risk R2.

### Illustrative v2 document

```yaml
version: 2
install: { name: my-spark-lab, install_dir: ~/AI, hosts: [127.0.0.1] }
models:
  qwen38-27b:
    active: true
    hf_model: RadixArk/Qwen3.8-27B-NVFP4
    image: lmsysorg/sglang:qwen38-27b
    hf_token_env: HF_TOKEN
    host: 0.0.0.0
    port: 30000
    min_nodes: 1
    resources: { mem_fraction_static: 0.85, node_assignment: auto, priority: normal }
    params: { kv_cache_dtype: fp8_e4m3, attention_backend: flashinfer }
    extra_flags: [ --enable-metrics ]
images:
  litellm: ghcr.io/berriai/litellm:latest
  pgvector: pgvector/pgvector:pg16
  redis: redis:7-alpine
  prometheus: prom/prometheus
  grafana: grafana/grafana
profile: prod
profiles:
  dev:
    images: { grafana: grafana/grafana:latest }
discovery:
  sources:
    - { alias: sparkrun, kind: sparkrun-registry, collection: default }
    - { alias: cookbook, kind: sglang-cookbook, repo: sglang/sglang }
conversion:
  enabled: false
  llm: { endpoint_env: CONVERSION_LLM_ENDPOINT, model: my-spark-model, api_key_env: LITELLM_MASTER_KEY }
litellm:    # v1 block, unchanged (model_name, port, *_env keys, db:, redis:, model_info:)
monitoring: # v1 block, unchanged
network:    # v1 block, unchanged
```

### Backwards compatibility (the v1 → v2 rule)

A config **without** `version:` is treated as v1. The compat loader upgrades it in memory, **render-invariantly**:

1. `model:` → `models.<recipe_name>:`, with `active: true` (default alias = the v1 `recipe_name`).
2. Every v1 field maps 1:1 into the model definition (no rename, no move); `resources` is left empty so defaults apply and `params.mem_fraction_static` keeps driving the SGLang flag.
3. `images:` is populated from the v1 per-service image fields; `profile`/`profiles`/`discovery`/`conversion` take defaults.
4. **Result:** rendering over a v1 config produces **byte-identical** files to the pre-branch engine. This is the R3 regression the parity tests assert (Phases 2/4).

`spark-lab migrate` performs the on-disk rewrite (v1 → v2) idempotently, preserving every value and adding `version: 2`; it is a convenience, never required — the compat loader makes the on-disk format optional.

## Consequences

- **Positive**
  - Multi-model is a config change, not a code path: adding a stored model is a new `models:` entry; making it live is `active: true` (or `active_models:`) plus a gated apply.
  - The image set is explicit, overridable three ways (env > profile > explicit), and validated before acceptance — Objective 8 complete.
  - `recipes/`, discovery, and conversion have a fixed, versioned home; adding a discovery source stays config-only (ADR 0003); conversion stays opt-in and safe.
  - V1 configs keep working unchanged; migration is opt-in via `spark-lab migrate`.
- **Negative / costs**
  - The schema is now *versioned*, so the loader branches on `version` and the compat path must stay byte-identical until v1 is formally retired post-deprecation.
  - `resources.mem_fraction_static` can duplicate a `params` value; the "resources wins, else params" rule must be documented and tested so the plan reports the effective value.
  - Three image-override layers (env / profile / explicit) enlarge the surface the availability check must enumerate.
- **Constraints honored**
  - Backwards compatible: v1 renders byte-identically; the schema is strictly additive.
  - No secrets in config: `*_env` naming throughout (`hf_token_env`, `api_key_env`, `endpoint_env`, registry tokens); values stay in `.env`.
  - Idempotent converge + gated model-restart: switching the active model still requires the explicit apply flag; nothing here auto-restarts the live model.
  - Legacy path preserved: the rendered-into-`install_dir` recipe path and the shell scripts are untouched by this schema.

## Alternatives considered

- **List-based `models:` (a list of maps with a `name` field).** Prettier in YAML, but keyed-by-alias is simpler to reference (`active_models:`, model selection, state, logs) and maps naturally to "one is active." Rejected in favor of a keyed map.
- **Keep one `model:` plus a `model_set:` sidecar for the others.** Retains the v1 shape but forks the schema into two shapes; the keyed `models:` map is a single source of truth. Rejected.
- **Per-model image map only, no top-level `images:`.** Model images would be in config, but the shared stack images would stay scattered per-service with no central override point; the top-level map plus profiles gives dev/test-vs-prod a single knob. Rejected.
- **Auto-apply a converted/active model without the flag.** Violates the gated-restart invariant and risks R1 (production disruption). Rejected: conversion output is a candidate; activation is always an explicit, gated apply.
- **Store the active model in state instead of config.** The active selection is declarative intent (what *should* run) and belongs in config; state records what *is* running (converge / ADR 0002). Merging them blurs the plan/state boundary. Rejected.
