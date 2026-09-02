# spark-lab — Design Contract (SPEC)

> This file is the **single source of truth** for the kit and the blog post.
> If you must deviate, record the deviation in this file's "Deviations"
> section. `config.example.yaml` is the authoritative schema for the
> user-facing config file — and it is the **only** example config in the repo
> (kept current with the v3 schema; no legacy-format examples are maintained).

## 1. Goals
- Anyone with a DGX Spark — or a cluster of Sparks managed by `sparkrun` — can
  self-host OpenAI-compatible LLM endpoints, monitor them, and optionally
  expose them, from **one config file for the whole cluster + one command**.
- **Idempotent + convergent:** re-running `spark-lab apply` after any change to
  `config.yaml`, the recipes, or templates brings every selected node to match
  config. No manual drift-chasing.
- **Portable & clean:** the public repo contains zero identifying hostnames,
  IPs, usernames, or secrets. Every identifying value is either a config knob
  or an env var — and the repo **mechanically enforces** this (see §8).

## 2. Fixed layout (exact paths)
```
spark-lab/
  README.md
  LICENSE                        # MIT
  .gitignore                     # ignore: .env(.*)*, config.yaml(.*)*, config.staging.yaml,
                                 # *.bak*, deploy/, .sparklab-state/, .venv/, labs/, ...
  config.example.yaml            # the ONLY example; current v3 schema, anonymized
  .env.example                   # secret knobs; .env is gitignored
  SPEC.md                        # this file
  pyproject.toml  uv.lock        # uv-managed env (fabric, jinja2, pyyaml, ...)
  bin/spark-lab                  # bash entrypoint -> managed venv python
  recipes/                       # plain sparkrun recipes = launch source of truth
  sparklab/
    cli.py                       # argparse entry + dispatch
    util.py
    commands/                    # one module per CLI verb
      init.py apply.py status.py teardown.py check.py adopt.py
      sync.py expose.py litellm.py model.py logs.py
    core/
      config.py                  # load+validate v3, env-ref + recipe resolution
      inventory.py               # live engine/gateway probe (status/sync/expose)
      cluster.py                 # host views, control-plane/monitoring roles,
                                 # prometheus targets, local-vs-remote detection
      recipes.py                 # recipe loading, layout pins, per-host views
      render.py                  # Jinja render (templates + view) -> deploy/
      converge.py                # build_plan: file diffs + ordered actions
      node.py                    # per-host execution seam (local vs remote)
      remote.py                  # Fabric/paramiko runtime + remote state
      runtime.py  schema.py
      state.py                   # node-side .sparklab-state/state.json
    templates/                   # Jinja: docker-compose.yaml.j2, litellm.env.j2,
                                 # litellm config/model_config/prometheus/grafana/...
  docs/                          # SETUP, ARCHITECTURE, OPERATIONS, MODEL_RECIPES,
                                 # NETWORKING, CLUSTERING, REMOTE_OPERATOR_MODE,
                                 # COMMANDS, adr/...
  scripts/                       # secret-scan.sh, flash-next-prepare.sh,
                                 # dgx_spark_diag.sh
  .githooks/pre-commit           # secret gate (git config core.hooksPath .githooks)
  .gitleaks.toml                 # gitleaks config (path allowlist + value rule)
  .github/workflows/validate.yml # CI: dry-run, render, compose, shellcheck,
                                 # tests+coverage, yamllint, secret scan
```

## 3. Config schema (v3, one file per cluster — ADR-0008)
Authoritative in `config.example.yaml`. Top-level sections: `version`,
`install`, `hosts`, `models`, `images`, `litellm`, `monitoring`, `network`.

- **Secrets are env-var *names*, not values.** e.g. `hf_token_env: HF_TOKEN`
  means "read `$HF_TOKEN` from `.env`" and inject it at render time. `init`
  writes `.env` from `.env.example` and generates keys with
  `openssl rand -hex 32`.
- **`hosts:`** — the managed nodes. Each entry: `name` (the `--hosts`
  identifier), `ssh` (connection target: Tailscale shortname,
  `user@magic-dns`, or IP), `remote: true|false`, and optional `ip:` — the
  tailnet IP sparkrun's placement should address the host by (sparkrun
  resolves layout pins / `--hosts` against cluster host IPs, not hostnames;
  without `ip:` the name is used). Any other key is a
  **per-host override**, deep-merged over the cluster-wide document
  (`install:`, `litellm:`, `monitoring:`, `images:`, ...). Two roles matter:
  - `control_plane.enabled` (default `true`) — hosts with the control plane
    host the LiteLLM gateway + DB/Redis + monitoring; models on other hosts
    are still served *through* it (implicit central serving: the gateway's
    `api_base` points at the running host).
  - `monitoring.role` — `full` (default, whole observability stack),
    `exporters` (host/GPU sidecars only; the full host's prometheus scrapes
    it), or `none`.
- **`models:`** — one entry per model: `active`, `hosts` (placement: absent =
  all hosts, `[]` = scaled down everywhere; this list **is** the scale),
  `recipe:` (which `recipes/<name>.yaml` the model is), `hf_token_env`, and
  optional `host_overrides.<host>` (deep-merged over the model for that
  host). At most ONE active model may serve a given host (enforced at load).
  - A recipe with `min_nodes > 1` **spans** its `hosts:` list: the first
    entry is the head (rank 0, where the workload is launched and probed),
    the rest join as workers (they converge control plane/monitoring but
    never launch the model).
- **Recipes are the launch source of truth (ADR-0009).** `recipes/*.yaml` are
  plain, directly-runnable sparkrun recipes (`sparkrun run recipes/x.yaml`
  works standalone). spark-lab renders a node-side copy that adds a
  `layout:` placement pin (from `hosts:`) + the HF token; gateway name and
  `model_info` (costs, caps, reasoning flags) come from the recipe's
  `metadata.litellm` section.
- **`install:`** — `name`, `install_dir` (where recipes + the litellm stack
  live on each node; `~/...` expands **on the node**), `repo_dir`
  (node-side checkout holding state).
- **`litellm:` / `monitoring:` / `network:` / `images:`** — defaults for the
  shared stack; env `SPARKLAB_IMAGE_<KEY>` overrides any image.
- Older schema shapes (v1/v2) are retired; `load` rejects them outright.

## 4. CLI contract (`bin/spark-lab`)
Bash wrapper: ensures a managed env via **uv** (`uv sync --no-default-groups`
from `pyproject.toml` + `uv.lock`; falls back to `python3 -m venv` +
`pip install -e .` when uv is absent), then `exec .venv/bin/python
-m sparklab.cli "$@"`.

| Command | Behavior |
|---|---|
| `init [--yes]` | Create `config.yaml` (from the example) + `.env` (from example, generated keys). Node prep (docker/tailscale/sparkrun) is plain ops. |
| `apply [--dry-run] [--hosts a,b] [--restart-model] [--diff]` | **Converge** every selected host to config (see §5). `--dry-run` is read-only and works even with hosts unreachable (degraded state + warning). `--restart-model` lifts the gate on model restarts. `--diff` (with `--dry-run`) shows per-file diffs. |
| `status [--hosts] [--json]` | Live view per host: sparkrun + compose + EVERY engine answering /v1/models (managed or hand-started) + the gateway's actually-served list + placement table. `--json` = one machine-readable object. |
| `model up <m> --hosts a,b` / `model down <m> --yes --hosts a,b` | Scale a model: add/remove hosts from its `hosts:` (rewrites config) + converge. Down keeps the recipe file on disk. |
| `model stop <m>` | Stop the model workload now (config unchanged; next apply restarts). |
| `check` | Config valid + renderable + binaries present on every selected host + boot-survival probe (restart policies, enabled-at-boot units). Read-only. |
| `sync [--write]` | PULL live reality: engines running but unexposed, gateway ghosts, missing models, file drift. `--write` adds extra_models entries + refreshes node state; model workloads never touched. |
| `expose <host[:port]> [--served-model] [--public-name] [--dry-run]` | Probe an engine's /v1/models, append a litellm.extra_models entry to config.yaml, converge gateways only (verified restart). |
| `litellm status\|restart` | Gateway control: staleness/health/served list; restart = write stale gateway files + compose restart + bounded health poll + state update. |
| `teardown [--yes] [--purge]` | Stop the model + remove the stack. Named docker volumes are destroyed **only** by `--purge`. |
| `adopt` | Take over an existing running install (read-only; writes only state). |
| `logs <service> [--hosts]` | Tail logs from a stack service. |

Global flags: `--config PATH` (default `./config.yaml`), `-v`, `--json`.
Every mutating command is safe under `--dry-run`.

## 5. Converge algorithm (`core/converge.py::build_plan` + `commands/apply.py`)
For each selected host (in config order):
1. **View.** Resolve the host's effective config: cluster-wide document +
   that host's per-host overrides + model placements (which models run here,
   what the gateway serves, exporter scrape targets).
2. **Render.** Templates + resolved secrets → `deploy/` for this host
   (litellm compose/config/.env/model_config/prometheus/grafana/scripts +
   the active models' node-side recipes with layout pins). Recipes may carry
   an `{install_dir}` placeholder in `executor_config` host-side paths (bind
   mounts); it is expanded to THIS node's concrete install dir when the file
   is written into it, so repo recipes stay machine-independent.
3. **State + diff.** Node-side state file `<repo_dir>/.sparklab-state/
   state.json`; classify every target file added/changed/removed.
4. **Actions, only for what changed:**
   - control-plane files changed → `docker compose up -d` + remove orphans
     (volumes persist; the rendered `litellm/.env` is the only 0600 file).
   - tailscale enabled → `systemctl enable --now tailscaled` (idempotent).
   - model workload: single-node → launch on this host; spanning → **head
     only** (`sparkrun run … --ensure --hosts <placement>` after `sparkrun
     setup ssh --hosts <placement>`; workers skip). Launch is detached; a
     bounded readiness probe follows (`metadata.readiness_seconds`, default
     10 min).
   - model restart is **gated**: a recipe change restarts only with
     `--restart-model` (the gate is skippable when the running container's
     `--ensure` intent is unchanged, e.g. a layout-pin addition).
   - model stop is **idempotent**: `sparkrun stop` fails when no workload
     matches the intent ("No running workload matches ...") -- that is the
     already-converged outcome, so the stop tolerates exactly that result
     (a stale state entry for a model that is not running never aborts the
     converge); every other stop failure still fails it.
5. Record new state; print a per-host summary. A real `apply` to an
   unreachable host fails clearly (executions raise); a `--dry-run` degrades
   to an empty remote state with a one-time warning instead of crashing.

## 6. Target install layout on a node
```
<install_dir>/
  sparkrun/recipes/<model-recipe>.yaml
  litellm/
    docker-compose.yml  config.yaml  model_config.yaml  .env  prometheus.yml
    grafana/provisioning/datasources/prometheus.yml
    grafana/provisioning/dashboards/dashboards.yml
    grafana/dashboards/{sglang-dashboard.json,spark-host-overview.json}
    scripts/nvidia-gpu-textfile.sh
<repo_dir>/.sparklab-state/state.json
```
`apply` (re)creates `<install_dir>`; it never deletes user data not owned by
the kit (teardown is the only destructive path, and it needs `--yes`).

## 7. Naming / ports reference
| Thing | Default | Where |
|---|---|---|
| Model serve port | per recipe (commonly 30000) | recipe `port` |
| LiteLLM gateway | 4000 | `litellm.port` |
| Postgres | 5432 (internal) | compose `db` |
| Redis | 6379 | `litellm.redis.port` |
| Prometheus | 9090 | `monitoring.prometheus.port` |
| Grafana | 3000 | `monitoring.grafana.port` |
| node_exporter / DCGM / cAdvisor | 9100 / 9835 / 8080 | compose |
| Prometheus `external_labels` | `hardware: gb10`, `host: <instance_label>` | templated |

## 8. Sanitization rules (repo must contain NONE of these — enforced)
- Personal names/usernames, real hostnames or Tailscale domains, IPs other
  than `127.0.0.1`, domains. Examples use `luna`/`sol` node names with
  `<tailnet>.ts.net`-style placeholders or shortnames.
- Any key/token/password value. Secrets = env-var **names** in config +
  `.env` (gitignored). `.env.example` uses obvious placeholders.
- Grafana dashboard JSONs: no host-specific labels; use the templated
  `{{ instance_label }}`.
- **Enforcement (mechanical, not aspirational):**
  1. `.gitignore` covers the sinks: `.env*`, `config.yaml*`,
     `config.staging.yaml`, `*.bak*`, `deploy/`, `.sparklab-state/`, `labs/`.
  2. The pre-commit **path gate** (`scripts/secret-scan.sh`) blocks staging
     any secret-sink path (`.env*`, `config.yaml`, `*.bak*`, generated
     trees) no matter what it contains.
  3. The **gitleaks value rule** (`env-credential-assignment` in
     `.gitleaks.toml`) flags literal credentials assigned to
     credential-named variables anywhere in scanned content — the class of
     miss (hyphenated `sk-*` keys, bare-hex passwords, renamed sinks) that
     gitleaks' built-in shape rules miss.
  4. CI runs the same `scripts/secret-scan.sh` as the last gate.

## 9. Template notes
- Recipes: keep `executor: docker` + the Blackwell/GB10 workarounds
  (`privileged: true`, `cap_add: [SYS_PTRACE]`,
  `security_opt: [seccomp=unconfined]`, `ipc: host`, `shm_size: 32g`).
  JSON serve-args in command templates must be single-quoted (the serve
  command runs via `bash -c`).
- litellm compose: services `litellm, db, redis (if enabled), prometheus (if
  monitoring.enabled), grafana, node_exporter, dcgm_exporter, cadvisor,
  gpu_textfile` (sidecars follow the monitoring role). GB10 node_exporter
  tweaks + the `nvidia-gpu-textfile.sh` sidecar. Grafana admin password from
  env.
- prometheus.yml: jobs `prometheus, sglang (host.docker.internal:<port>, with
  metric_relabel)`, node (full role: localhost; exporter role: scraped via
  the host's `ssh` address), dcgm, cadvisor. Generous scrape timeouts on GB10.
- litellm model_config: one entry per served model, `api_base` rendered from
  placement at apply time (gateway-static), `api_key: not-needed`,
  `model_info`/`litellm_settings` from the recipe's `metadata.litellm` +
  config defaults.

## 10. Verification checklist (CI runs all of this)
- [x] `apply --dry-run` against `config.example.yaml` renders every template
      and prints a plan (works with the hosts unreachable: degraded state).
- [x] Rendered `docker-compose.yml` passes `docker compose config -q`.
- [x] Rendered recipes + all YAML parse; `shellcheck` clean on shell files.
- [x] Grep-auditable: no usernames, real hostnames/domains, real IPs, real
      keys — see §8 enforcement.
- [x] Test suite (unit/integration/regression/parity) + coverage ≥ 85%.
- [x] Secret scan (gitleaks, pinned version) clean.

## 11. Blog post
- File: the Astro blog's content collection
  (`src/content/blog/dgx-spark-llm-lab.md`), voice per the blog's
  `DESIGN.md` (warm, editorial, prose-first, quiet chrome).
- **Narrative beats:** (1) the itch — running a real model on a quiet desktop
  box; (2) the stack in plain terms (Spark → vLLM/SGLang → LiteLLM gateway →
  Prometheus/Grafana → Tailscale); (3) the payoff: one config file +
  `spark-lab apply`, and it *converges* when you change things; (4) what you
  get (a Grafana you can actually read, private access); (5) "run it on your
  own Spark" with a link to the repo.
- Terminal snippets in the blog were captured read-only before inclusion
  (the capture helper script was retired with the staging rig).

## 12. Deviations / history
- Schema evolution: v1 `model:` → v2 `models:` → **v3 cluster
  `hosts:` + per-model `hosts:`/`host_overrides`** (ADR-0008). v3 is now the
  ONLY accepted schema (`load` rejects anything else); the v1/v2 compat
  loader + `migrate` were retired once every live config was v3.
- **Recipes as source of truth + structural layout pins** (ADR-0009): the
  config references plain sparkrun recipes; spark-lab adds the placement pin
  + secret at render time; gateway metadata lives in `metadata.litellm`.
- **Spanning models** (`min_nodes > 1`): the model's own `hosts:` list is its
  run pool; head-only launch; per-model pool (no global `install.hosts`
  migration).
- **Remote operator mode**: the same config converges locally or over SSH
  (Fabric); node-side state at `<repo_dir>/.sparklab-state`; per-host
  control-plane + monitoring roles decouple "where the gateway lives" from
  "where the model runs".
- `lib/` → `sparklab/` package (PEP 420 layout, uv-managed); the CLI grew
  past the original five verbs and was then cut back to the core verbs +
  `model`/`logs` (see docs/COMMANDS.md for the full keep/cut inventory).
