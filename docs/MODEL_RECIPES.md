# Model recipes

## Recipes are the source of truth (v3)

In a v3 cluster config, a model block is **placement, not a launch spec**:

```yaml
models:
  my-model:
    active: true
    hosts: [luna]                 # where it runs (this list IS the scale)
    recipe: qwen38-27b            # -> <config dir>/recipes/qwen38-27b.yaml
    hf_token_env: HF_TOKEN        # which .env var carries the HF token
```

`recipes/<name>.yaml` is a **plain, directly-runnable sparkrun recipe** —
no spark-lab-specific keys (the test suite enforces this against the
sparkrun 0.3.4 key set). A user can `sparkrun run recipes/<name>.yaml`
without spark-lab at all: the scheduler auto-places it, and gated models
authenticate host-side (`hf auth` — sparkrun pre-distributes weights and
runs containers HF-offline, so no token belongs in the file).

The recipe carries the whole launch spec:

- **`model:` / `runtime:` / `min_nodes:` / `container:`** — engine identity.
- **`executor_config:`** — Docker flags. On the GB10 you need
  `privileged: true`, `cap_add: [SYS_PTRACE]`, `security_opt:
  [seccomp=unconfined]`, `ipc: host`, and a large `shm_size` (the SGLang
  build hits a Blackwell issue otherwise — see
  https://github.com/radixark/miles/issues/364).
- **`defaults:`** — `{placeholder}` → value map (incl. `host:`/`port:`).
- **`command:`** — the literal `sglang serve ...` line with `{placeholders}`
  (keep `--enable-metrics` so the dashboards keep working).
- **`env:`** — container env vars (no secrets; the HF token is injected by
  spark-lab at render time from `hf_token_env` + `.env`).
- **`model_revision:`** — optional pinned checkpoint revision (pair it with
  a `--revision` flag).
- **`metadata:`** — sparkrun's documented free-form section; spark-lab reads
  two extensions from it (sparkrun ignores them):
  - `metadata.litellm:` — gateway metadata: `model_name` (the gateway's
    client-facing name) + `model_info` (costs, token limits, reasoning/
    vision support). This is what the LiteLLM `model_list` entry renders
    from — including per-token costs, so spend tracking is meaningful.
  - `metadata.readiness_seconds:` — the apply-time `/health` probe bound
    (default 600; slow first boots raise it — Flash-Next uses 5400 for the
    48 GB PLE-table fill).

**What `apply` renders.** The node-side copy
(`<install_dir>/sparkrun/recipes/<alias>.yaml`) is the repo recipe plus
exactly two cluster-shaped additions, both known sparkrun keys:

- a **`layout:` placement pin** from `hosts:` (single-host models pin the
  run pool's default address, rank 0; spanning models pin each placement
  host in config order — rank 0 = first host = head node). sparkrun honors
  an explicit layout verbatim on every scheduler, so a gateway-served model
  can never be scheduler-moved to the wrong host.
- the **`HF_TOKEN` env value** (secret, render-time only).

The rendered file is therefore also a valid sparkrun recipe — it is the
file `sparkrun run/stop --ensure` addresses by path. See ADR-0009.

**Per-host tailoring** stays in the config: `host_overrides.<host>`
deep-merges over the model for that host (params, image, litellm serving
identity — e.g. a distinct gateway `model_name` on one host). `spark-lab
status` prints the derived host → model table (the inverse of `hosts:`).

## SGLang tuning knobs

Tuning lives in the recipe's `defaults:` + `command:`. The knobs that matter
most:

Some models need more than the generated `sglang serve` block and the base
`executor_config`. These optional keys render into the recipe (all absent →
byte-identical default render):
- `model.serve_command` → replaces the generated serve block entirely
  (`{model}` / `{host}` / `{port}` placeholders still substitute). The Flash-Next
  recipe uses this for its ~35-flag command line.
- `model.executor_config` → keys merged **over** the base block (`ipc`,
  `shm_size`, `privileged`, `cap_add`, `security_opt`); whole-key replacement,
  e.g. `user: "$SHELL_USER"` (non-root container; `$SHELL_USER` is sparkrun's
  variable, expanded to the SSH user), `memory_limit: 116g`,
  `volumes:` (host bind mounts — `src:dst:ro` supported). Sources may use the
  `{install_dir}` placeholder: spark-lab expands it to the node's real
  install dir when writing the node-side recipe (direct sparkrun users:
  replace it with an absolute path). Quote volume strings that start with it
  — an unquoted YAML scalar beginning with `{` parses as a flow mapping.
- `model.env` → extra container env vars (added after the base `PYTORCH_*`
  and the injected `HF_TOKEN`).
- `model.model_revision` → pinned HF checkpoint revision (top-level recipe
  field; pair it with a `--revision` flag in the serve command).
- `model.readiness_seconds` → the apply-time `/health` probe bound
  (default 600; slow first boots raise it — Flash-Next uses 5400 for the
  48 GB PLE-table fill).

The static registry recipes under `recipes/` (incl. `recipes/qwen38-flash-next.yaml`
and its `recipes/flash-next/patches/`) are the same content in
standard sparkrun-registry form for direct `sparkrun run` use; the cluster
config is the source of truth for what `apply` converges, so keep the two in
step when you tune one. `.sparkrun/registry.yaml` is a static human-facing
INDEX of those recipes (name → purpose → path) -- kept for reference since
the discovery subsystem that once consumed it was retired.

## SGLang tuning knobs (the example `params`)

The shipped `params` is a working example for a ~27B NVFP4 model. The knobs that
matter most:

- `mem_fraction_static` — fraction of unified memory reserved for static
  allocations. Raise toward ~0.9 if the model + KV cache fit; lower it if SGLang
  OOMs.
- `kv_cache_dtype` — `fp8_e4m3` (smaller, faster) vs `auto`/`float16` (exact).
- `chunked_prefill_size`, `max_running_requests` — batch/prefill behavior; lower
  them to reduce latency spikes under load.
- `attention_backend` — `flashinfer` is the default here.
- `reasoning_parser` / `tool_call_parser` — how SGLang splits reasoning + tool
  call output for the model family (`qwen3` / `qwen3_coder` in the example).
- speculative decoding — `speculative_algorithm: DSPARK` plus a draft model
  (`speculative_draft_model`) speeds up generation; `mamba_*` settings are
  specific to this model's hybrid attention/SSM architecture.

## Legacy: inline model blocks

v1/v2 configs and v3 blocks **without** a `recipe:` key still declare the
launch spec inline in the model block (documented-deprecated in v3). The
inline form's mapping:

- `model.hf_model` → the recipe's `model:` field; `model.image` →
  `container:`; `model.port` / `model.host` → `defaults.port` / `defaults.host`.
- each `model.params` key → a `defaults:` entry **and** an
  `--<key-with-dashes> {key}` flag (e.g. `kv_cache_dtype: fp8_e4m3` →
  `--kv-cache-dtype {kv_cache_dtype}`).
- `model.flag_map` → override a flag's name when it doesn't follow the
  underscore→dash rule.
- `model.extra_flags` → appended verbatim (`--enable-metrics`,
  `--trust-remote-code`, ...).

New work should use the reference form above; a model that outgrows the
generated serve block just gets a literal `command:` in its recipe file.

## Swapping the model

The simplest path: point the placement entry at a new `recipe:` (write or
pick a `recipes/<name>.yaml`) and adjust `hosts:` / gateway metadata in the
recipe's `metadata.litellm:`. Keep `--enable-metrics` in the `command:` so
the dashboards keep working.

For hands-free trying of new models, register the recipe as a **zoo model**
(`swap.enabled` on an inactive model block): llama-swap loads it on request
and unloads it when idle -- see [ADR-0010](adr/0010-zoo-model-swapping.md)
and the OPERATIONS "Model zoo" runbook.

For gated models, set `HF_TOKEN` in `.env` (and keep `hf_token_env` pointing
at it); spark-lab injects it into the rendered recipe at deploy time. It is
never written to the repo.

## Multi-node

For a cluster, set `install.hosts` to every node and `model.min_nodes` to the
number of nodes the recipe needs. `spark-lab apply` then builds the SSH mesh
(`sparkrun setup ssh`), creates the named cluster, and runs the recipe with
`--cluster <name>` instead of on a single host.
