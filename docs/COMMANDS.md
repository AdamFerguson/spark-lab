# spark-lab — command & config inventory

Every command, option, and config key with its redesign verdict. The design
landed here in full: all **CUT** items are gone, all **NEW** verbs shipped,
and `.sparkrun/registry.yaml` survives as a static, human-facing recipe
index (no code consumes it). Verdicts: **KEEP** (unchanged), **MOD** (kept,
changed), **CUT** (removed), **NEW** (added).

The redesign target, in one line: *encapsulate the current state of the
sparks (`status`), pull reality back into config (`sync`), edit config and
push it (`apply`), expose any running engine via the gateway (`expose`),
manage the gateway directly (`litellm`).* Nothing more complicated.

## Global options (accepted by every node-targeting command)

| Flag | Meaning | Verdict |
|---|---|---|
| `--config PATH` | config file (default `./config.yaml`) | KEEP |
| `--hosts a,b` | restrict to named entries of the config's `hosts:` (default all) | KEEP |
| `-v, --verbose` | verbose output | KEEP |
| `--json` | machine-readable output (honored by `status`, `check`) | KEEP |

## Commands

### `init [--yes] [--all]`  — verdict: MOD
Creates `config.yaml` + `.env` with freshly generated secrets (or, with
`--hosts`, idempotently *bootstraps* those nodes: run the system precheck and
install missing tools, clone/fast-forward the spark-lab checkout from
`install.repo_url`, create the install dir, bring tailscale up).
`--yes` = non-interactive; `--all` = also install optional tools.
**MOD:** keep the scaffold; cut the `--hosts` bootstrap + installer path (one
`check --system --install` remains for new hosts).

### `apply [--dry-run] [--diff] [--restart-model] [--no-model] [--apply] [--yes]`  — verdict: KEEP (MOD)
The converge engine, per selected host: render → diff rendered files against
the node's `.sparklab-state/state.json` → write changed files → on
control-plane change `docker compose up -d` (+ orphan removal; named volumes
persist) → tailscale `systemctl enable --now tailscaled` → cloudflare
`systemctl enable --now cloudflared` when enabled → model workload via
`sparkrun run --ensure` (spanning recipes: head only) or a tolerant
`sparkrun stop` for models no longer placed here; bounded `/health` readiness
probe after launch.
- `--dry-run` plan only (+ `--diff` per-file diffs)
- `--restart-model` allow the destructive model stop/restart on recipe change
  (without it the change stays *pending* and keeps being reported)
- `--no-model` converge control plane + gateway only; never launch/stop a
  sparkrun model (use when the model runs outside spark-lab)
- `--apply` / `--yes` hidden legacy no-ops → **CUT**
**MOD:** the litellm restart triggered by gateway-file changes currently
omits `extra_models.yaml` from its trigger set and is best-effort-silent →
fixed so any `litellm/` gateway change restarts litellm and then verifies
`/health` + the served model list.

### `status`  — verdict: MOD
Prints the derived placement table (from `models.<m>.hosts`), then per host a
raw passthrough: `sparkrun status`, `docker compose ps`, `tailscale status`,
and `systemctl is-active cloudflared` (when cloudflare enabled).
**MOD:** consolidated live inventory — per host: sparkrun workloads,
*unmanaged* engine containers (detected via `docker ps` + `GET /v1/models`
probe: served model names + port), the gateway's served model list, stack +
tailscale/cloudflared state; `--json` emits the whole thing machine-readable.

### `teardown [--yes] [--purge]`  — verdict: KEEP
Stop the model + `compose down` (volumes survive). `--purge` additionally
removes named volumes (per-volume warnings; the LiteLLM Postgres data is
unrecoverable). Requires `--yes`.

### `upgrade [--yes]`  — verdict: CUT
Refreshed per host: spark-lab checkout, `sparkrun` uv tool + recipe
registries, stack images, then re-apply with restart allowed. **Cut** —
replaced by docs: `uv tool upgrade sparkrun` (+ pull the checkout) then
`spark-lab apply --restart-model`.

### `check [config|images|system] [--images] [--probe] [--system] [--install] [--all]`  — verdict: MOD
Consolidated pre-flight: `check` (= `check config`) validates schema, secret
resolution, host set, per-host render, required binaries on each node;
`--images` resolves stack images per the precedence chain (`--probe` runs
`docker manifest inspect` per host); `--system` reports required/optional
tools (uv, python3, docker, git, curl, sparkrun; optional tailscale,
cloudflared, gitleaks) and `--install` (`--all`) runs sudo-aware installers.
**MOD:** keep the config/render/binary check and add a "stack survives
reboot" check (docker + tailscaled enabled-at-boot, container restart
policies); cut `--images`/`--probe`/`--system`/`--install`/`--all` (manual
commands documented; the tool table + installers go with them).

### `validate` (hidden alias of `check`)  — verdict: CUT
### `doctor [--install] [--all]` (hidden alias of `check --system`)  — verdict: CUT

### `migrate [--dry-run]`  — verdict: CUT
Rewrites a v1/v2 `config.yaml` to schema v3 (value-preserving; comments not
preserved). **Cut** once all live configs are v3 (one-off rewrite of
`labs/*/config.yaml` during the redesign).

### `adopt [--dry-run]`  — verdict: CUT (absorbed by `sync`)
Read-only against the live install: compares every rendered file to what's on
disk, records on-disk reality into the node's state file (writes ONLY state;
runs no sparkrun command), reports drift. After `sync` lands, `sync --write`
covers the same need (state refresh + config pull-back).

### `model up <model> [--hosts a,b]`  — verdict: KEEP
Adds host(s) to `models.<model>.hosts` (rewrites config.yaml) and converges
the new hosts. Refuses when two active models would share a host.

### `model down <model> --yes [--hosts a,b]`  — verdict: KEEP
Removes host(s) from the model's `hosts:` and converges, stopping the workloads
no longer placed; the dropped host's recipe file is left on disk (inert).
Fully scaled down ⇒ `hosts: []` and `active` dropped.

### `model stop --yes`  — verdict: KEEP
Stops the active model workload now (tolerant `sparkrun stop`; state records
the stop; next `apply` starts it again). Config unchanged.
*Naturally sparkrun-only — manual models are stopped on the node itself.*

### `recipes search <query> [--source S]` / `recipes list [source]` / `recipes show <ref>` / `recipes convert <ref> [--out PATH] [--dry-run]`  — verdict: CUT
Discovery subsystem (ADR-0003): fans queries over the sparkrun registry and
the SGLang cookbook, converts hits into candidate recipes under
`recipes/candidates/`. Never used in practice; −735 LOC + `cookbook/` +
docs/REGISTRY.md + ADR-0003. (`.sparkrun/registry.yaml` was restored
afterward as a static, human-facing recipe INDEX -- no code consumes it.)

### `logs <service> [--lines N] [-f]`  — verdict: KEEP
Tail `docker compose logs` for `litellm|db|redis|prometheus|grafana` (one
host; `--hosts` to pick). Default 100 lines; `-f` follows.

## New commands (redesign)

### `status --json` (see MOD above) — NEW behavior
### `sync [--write]` — NEW
PULL half of the loop: reads live reality from every host (sparkrun status,
engine containers via `/v1/models`, gateway model list, on-disk recipe
hashes) and diffs it against `config.yaml`. Reports: engines running but not
exposed (with a ready-to-write `extra_models` entry), exposed/active models
that are NOT running, recipe files on the node that differ from the repo
copies. `--write` applies the safe parts: appends exposure entries for
discovered engines + refreshes the node state file (absorbing `adopt`);
recipe drift is only ever *reported* (you decide, since recipes are
git-managed).

### `expose <name> --host H[:port] [--served-model M] [--public-name N] [--api-key-env E] [--model-info k=v]…` — NEW
One command to put ANY reachable OpenAI-compatible engine behind the gateway:
probes `H/v1/models` (defaults `:8000`) for the served model id, writes a
well-formed `litellm.extra_models` entry into `config.yaml`, converges the
gateway files only, restarts litellm + verifies. `litellm.extra_models` stays
editable by hand too (raw LiteLLM `model_list` entries) — `expose` is sugar.

### `litellm status|restart` — NEW
- `litellm status`: gateway health, served model list, config file hash vs
  render (is it in sync?).
- `litellm restart`: re-render gateway files, write if changed, `compose
  restart litellm`, poll `/health`, print the served model list. The manual
  restart you've been doing, made one command and verified.

## Config surface (`config.yaml`, schema v3)

| Key | Meaning | Verdict |
|---|---|---|
| `version: 3` | schema version | KEEP (only accepted version after the cut) |
| `install.name` | lab name (tailscale/cli branding) | KEEP |
| `install.install_dir` | node install dir (`~` allowed; `{install_dir}` recipe placeholder resolves against it) | KEEP |
| `install.repo_dir` | node-side spark-lab checkout (remote state lives here) | KEEP |
| `install.repo_url` | clone source for `init --hosts` bootstrap | CUT (with bootstrap) |
| `hosts: [{name, ip, ssh, remote, user, port, identity_file}]` | managed nodes; `ssh`/`remote` = converge over SSH, `ip` = sparkrun placement address | KEEP |
| `models.<alias>.active` | participate in placement/validation | KEEP |
| `models.<alias>.recipe` | `recipes/<name>.yaml` identity | KEEP |
| `models.<alias>.hosts` | placement list (first = spanning head); `[]` = parked | KEEP |
| `models.<alias>.hf_token_env` | env/.env var for the HF token injected at render | KEEP |
| `models.<alias>.serve_command` / `.executor_config` / `.env` / `.model_revision` / `.readiness_seconds` | recipe overrides rendered into the node-side sparkrun recipe | KEEP |
| `models.<alias>.host_overrides.<host>` | per-host tailoring (`litellm:` identity etc.) | KEEP |
| `images: {…}` | per-service image overrides (single layer) | KEEP |
| `profile` / `profiles:` | image-precedence layer (v2 era) | CUT |
| `litellm.{model_name, port, model_api_base_host, master_key_env, salt_key_env}` | gateway identity + secrets-by-env | KEEP |
| `litellm.db.{user, password_env, db}` / `litellm.redis.{enabled, port}` | gateway store | KEEP |
| `litellm.model_info.{supports_vision, supports_reasoning, max_input_tokens, max_output_tokens, supported_reasoning_efforts, *_cost_per_token}` | advertised model capabilities/costs | KEEP |
| `litellm.extra_models` | raw LiteLLM `model_list` entries for externally-run engines | KEEP (+ `expose` sugar) |
| `monitoring.{enabled, role: full\|exporters\|none, prometheus.{port, retention}, grafana.{port, admin_password_env}, instance_label, dashboards}` | observability stack | KEEP |
| `network.tailscale.enabled` | ensure tailscaled on nodes | KEEP |
| `network.cloudflare.{enabled, tunnel_token_env, public_hostname}` | ensure the system `cloudflared` service; docs/NETWORKING.md | **KEEP (owner decision)** |
| v1 `model:` block, `active_models`, v1 per-service image fields | legacy schema | CUT |
| `install.remote` / `install.hosts` (v2 shapes) | superseded by `hosts:` | CUT (config keys still *read* by nothing after the cut) |

## Files/docs pruned by the redesign

- `scripts/staging.sh`, `scripts/capture.sh`, `docs/STAGING_E2E.md` (staging rig)
- `cookbook/`, `.sparkrun/registry.yaml`, `docs/REGISTRY.md`, `docs/adr/0003-*` (discovery)
- `docs/MIGRATION_PLAN.md`, `docs/V3_ROLLOUT.md`, `docs/EXISTING_SPARK_MIGRATION.md`,
  `docs/AUDIT-P6.md`, `docs/ROADMAP.md`, `GLM53-EXL3-CONVERSION-STATUS.md`
  (migration-era one-offs; git history preserves them)
- untracked working-dir junk: `staging-report/`, `lib/__pycache__/`
- kept docs (rewritten in phase 4): README, SPEC, OPERATIONS, SETUP,
  MODEL_RECIPES, CLUSTERING, NETWORKING, REMOTE_OPERATOR_MODE, ARCHITECTURE,
  ADR 0001/0002/0005/0006/0007/0008/0009
