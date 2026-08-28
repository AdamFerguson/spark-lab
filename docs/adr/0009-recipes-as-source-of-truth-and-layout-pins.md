# ADR-0009: Recipes as source of truth; placement pins via sparkrun `layout`

Date: 2026-08-28 · Status: accepted · Supersedes: nothing (extends ADR-0008)

## Context

Since ADR-0008, a v3 `config.yaml` model block carried the entire launch
spec inline (`hf_model`, `image`, `params`, `extra_flags`, `serve_command`,
`executor_config`, `env`, `model_revision`, `readiness_seconds`,
`litellm:`…), while `recipes/*.yaml` held a near-duplicate "registry twin"
for the discovery commands. The result: two sources of truth for the same
launch, one of them (the twin) going stale, and a `config.yaml` whose model
sections were walls of engine detail.

Separately, placement was only *coincidentally* pinned: with no
`install.hosts`, converge runs `sparkrun run <recipe> --ensure
--hosts 127.0.0.1` on the target node, so the scheduler's pool is the local
node alone. Nothing stops a future `install.hosts` / `--cluster` setup from
handing a gateway-served model's placement to the occupancy scheduler —
which could move a live service between hosts, and would break the gateway
(config rendered statically from `hosts:`).

## Decision

**1. `config.yaml` model blocks become placement-only.** A v3 model block
references the recipe instead of re-declaring it:

```yaml
models:
  my-model:
    active: true
    hosts: [luna]
    recipe: qwen38-27b        # -> <config dir>/recipes/qwen38-27b.yaml
    hf_token_env: HF_TOKEN    # which .env var carries the HF token
```

Cluster-shaped values stay in config on purpose: placement (`hosts`/
`active`), the token env-var *name* (a deployment credential concern), and
`host_overrides` (per-host tailoring — the escape hatch). At load,
`core.recipes.resolve_all` folds the referenced recipe's launch spec into
the model block (inline keys win), so render/converge/`serving_entries`
see one resolved dict and never branch on the reference form. Inline v3
blocks without `recipe:` keep working (documented-deprecated; v1/v2
untouched), so the migration is incremental.

**2. `recipes/*.yaml` are plain, directly-runnable sparkrun recipes — no
spark-lab-specific keys.** Only sparkrun-known top-level keys
(`sparkrun_version … layout`, mirrored in `core.recipes.SPARKRUN_KNOWN_KEYS`
from sparkrun 0.3.4 and validated by the test suite). Consequences:

- anyone can `sparkrun run recipes/<name>.yaml` without spark-lab — the
  scheduler auto-places it; gated models authenticate host-side
  (`hf auth`), since sparkrun pre-distributes weights and runs containers
  HF-offline;
- no secret ever lives in a repo recipe (no placeholder markers: sparkrun
  has no `{env:...}` syntax and uses recipe `env` values literally);
- spark-lab's own extensions live in sparkrun's documented free-form
  `metadata:` section: `metadata.litellm:` (gateway name +
  `model_info` — costs, limits, reasoning) and
  `metadata.readiness_seconds:` (the apply-time `/health` probe bound).
  sparkrun reads `description`/`maintainer` from `metadata:` and ignores
  the rest, so the files stay valid.

**3. Render-time injection, never repo-time.** The node-side rendered
recipe (`<install_dir>/sparkrun/recipes/<alias>.yaml`) adds exactly two
cluster-shaped things, both known sparkrun keys:

- a `layout:` placement pin from the placement entry's `hosts:` —
  single-host models pin the run pool's default address (rank 0);
  spanning models (`min_nodes: > 1`) pin each placement host in config
  order (rank 0 = first host = head node, per sparkrun's contract).
  Placement hosts must appear verbatim in the run pool (`install.hosts`);
  `len(hosts) >= min_nodes` is validated at load;
- the `HF_TOKEN` env value (from `hf_token_env` + `.env`), rendered with
  its historical explicit quoting.

The rendered file is *also* a fully valid sparkrun recipe (the file
`sparkrun run/stop --ensure` already addresses by path today).

**4. Placement stays model→host; the host side is a derived view.**
`spark-lab status` prints a read-only host→model table
(`Config.placement_table()`): which model serves which host, its gateway
name, and the host's control-plane state. No host-side `models:` list:
placement belongs to the workload (sparkrun's own explicit-placement
primitive is recipe-scoped `layout:`), one model spanning hosts is only
expressible model-side, one edit site keeps `model up/down` verbs
mechanically correct, and a second stored form could drift from the first.

## Rationale (verified against sparkrun 0.3.4 source + docs)

- **`layout` is the scheduler's own explicit-placement primitive.**
  `schedulers/greedy.py` + `_occupancy_base.py`: every scheduler honors an
  explicit layout verbatim; a placement host missing from the cluster host
  list raises `PlacementError` naming the hosts; the layout must cover all
  ranks. Pinning therefore does not fight the scheduler — it is the
  scheduler's explicit mode. Transient, non-gateway experiments (the
  scheduler's real use case) simply omit a layout and stay scheduler-placed.
- **The gateway demands static placement.** `litellm/model_config.yaml`
  renders `api_base` from the placement decision at apply time; a
  run-time scheduler pick could move a live service and leave the gateway
  pointing at an idle host. Served models must be pinned; only
  gateway-less transients may be scheduler-picked.
- **Migration is `--ensure`-safe.** `sparkrun run --ensure` matches a
  running job by intent-id = hash of `runtime + model + container + port +
  served-name + parallelism` only (`orchestration/job_metadata.py`) —
  serve args, `layout`, `env`, `metadata` are **not** hashed. Adding the
  layout pin to the on-disk recipe cannot defeat ensure-matching or spawn a
  duplicate. spark-lab's own restart gate (sha256 of the whole rendered
  recipe) does trip once on the layout addition — skippable, since the
  running container's intent is unchanged.
- **Unknown top-level recipe keys are swept into `runtime_config`, not
  rejected** — but we do not rely on that: the compliance test rejects
  unknown keys so a future typo fails loudly instead of vanishing.
- **`metadata:` is the documented free-form extension section** ("v2
  extension for VRAM estimation, model info"), which is why the gateway
  metadata goes there instead of an invented top-level key.

## Consequences

- The live cluster config's one-time apply changes sol's
  `litellm/model_config.yaml` by removing a latent bug: sol's host-level
  `model_info` (27B costs + reasoning-effort flags) used to deep-merge into
  *every* gateway entry on that host, so the nemotron entry claimed the
  27B's costs. After the move, each entry carries its own recipe's
  metadata. Expect one best-effort litellm restart on the next sol apply.
- Both recipe files and their on-disk rendered copies remain directly
  usable with `sparkrun` (stop/status/run by path).
- `config.yaml` `active_models:` remains a v2 vestige: harmless now
  (validated before being honored as the base representative, ADR-0008
  follow-up `b2e21bf`), deprecated in v3 examples.
- Follow-ups (out of scope here): removing inline v3 model blocks entirely;
  `adopt` emitting reference-form configs.
