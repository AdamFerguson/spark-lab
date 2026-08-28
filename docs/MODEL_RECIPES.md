# Model recipes

spark-lab generates a **sparkrun recipe** from the `model` section of
`config.yaml`. A recipe is a YAML file that tells sparkrun *what container to
run, with what env, and what command line to serve the model with*.

## How the template maps to `sglang serve`

The recipe has three parts:

- **`defaults:`** — a map of `{placeholder}` → value. sparkrun substitutes
  `{name}` in the command with the value from `defaults`.
- **`command:`** — the literal `sglang serve ...` line, referencing
  `{placeholders}`.
- **`executor_config:`** — Docker flags. On the GB10 you need `privileged: true`,
  `cap_add: [SYS_PTRACE]`, `security_opt: [seccomp=unconfined]`, `ipc: host`,
  and a large `shm_size` (the SGLang build hits a Blackwell issue otherwise —
  see https://github.com/radixark/miles/issues/364).

From `config.yaml`:

- `model.hf_model` → the recipe's `model:` field → substituted for `{model}` in
  `--model-path {model}`.
- `model.image` → `container:`.
- `model.port` / `model.host` → `defaults.port` / `defaults.host`.
- each `model.params` key → a `defaults:` entry **and** an
  `--<key-with-dashes> {key}` flag. Example: `kv_cache_dtype: fp8_e4m3` becomes
  `defaults.kv_cache_dtype: fp8_e4m3` plus `--kv-cache-dtype {kv_cache_dtype}`.
- `model.flag_map` → override a flag's name when it doesn't follow the
  underscore→dash rule. The example maps
  `speculative_draft_model → speculative-draft-model-path`.
- `model.extra_flags` → appended verbatim (this is where `--enable-metrics`,
  `--trust-remote-code`, etc. live).

### Advanced per-model recipe overrides (optional)

Some models need more than the generated `sglang serve` block and the base
`executor_config`. These optional keys render into the recipe (all absent →
byte-identical default render):

- `model.serve_command` → replaces the generated serve block entirely
  (`{model}` / `{host}` / `{port}` placeholders still substitute). The Flash-Next
  recipe uses this for its ~35-flag command line.
- `model.executor_config` → keys merged **over** the base block (`ipc`,
  `shm_size`, `privileged`, `cap_add`, `security_opt`); whole-key replacement,
  e.g. `user: "$SHELL_USER"` (non-root container), `memory_limit: 116g`,
  `volumes:` (host bind mounts — absolute node paths; `src:dst:ro` supported).
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
step when you tune one.

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

## Swapping the model

The simplest path: change `model.hf_model`, `model.image`, and the `params`
block to match your model. If a flag's name differs from its param key, add a
`model.flag_map` entry. Keep `--enable-metrics` in `extra_flags` so the
dashboards keep working.

For gated models, set `HF_TOKEN` in `.env` (and keep `hf_token_env` pointing at
it); spark-lab injects it into the container at deploy time. It is never written
to the repo.

## Multi-node

For a cluster, set `install.hosts` to every node and `model.min_nodes` to the
number of nodes the recipe needs. `spark-lab apply` then builds the SSH mesh
(`sparkrun setup ssh`), creates the named cluster, and runs the recipe with
`--cluster <name>` instead of on a single host.
