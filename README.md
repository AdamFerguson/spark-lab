# spark-lab

Self-host an OpenAI-compatible LLM on an **NVIDIA DGX Spark** — or a cluster of
Sparks managed by [`sparkrun`](https://github.com/scitrera/sparkrun) — with a
LiteLLM gateway, Prometheus + Grafana monitoring, Tailscale for private access,
and optional Cloudflare Tunnel for public sharing.

One config file plus one command:

```bash
git clone <this-repo> spark-lab && cd spark-lab
./bin/spark-lab init          # create config.yaml + .env, generate keys
# edit config.yaml and .env for your hosts / model / ports / dashboards
./bin/spark-lab apply --dry-run   # preview the plan
./bin/spark-lab apply             # deploy + converge
```

`apply` is **idempotent and convergent**: it re-renders the stack from your
config, diffs what changed since the last run, and only re-acts on the
difference. Change the config or a template, run `apply` again, and the host(s)
converge to the new state.

## What it does

| Piece | Role |
|---|---|
| **sparkrun** | Launches the model (SGLang) as a container; handles single-node and cluster. |
| **SGLang** | Serves the model; an OpenAI-compatible API on `:30000`. |
| **LiteLLM** | Unified gateway on `:4000` — keys, spend tracking, model routing. |
| **Prometheus + Grafana** | Observability: model metrics, GPU, host, per-container. |
| **Tailscale** | Private-network access to the gateway from any of your devices. |
| **Cloudflare Tunnel** | *(optional)* public access for selected friends/colleagues. |

## Getting started

```bash
git clone <your-fork> spark-lab && cd spark-lab
git config core.hooksPath .githooks      # install the pre-commit secret gate (once)
./bin/spark-lab init                       # create config.yaml + .env (v3 example), generate keys
# edit config.yaml: hosts, model, ports, dashboards, network
./bin/spark-lab check                      # pre-flight (config + per-host render + binaries)
./bin/spark-lab apply --dry-run            # see the plan; touches nothing
./bin/spark-lab apply --hosts <your-host>  # materialize: converge the selected host(s)
```

## Several Sparks, one config

A `version: 3` config describes the **whole cluster** — every managed node is
an entry under `hosts:`, and every command takes `--hosts a,b` to scope itself
(unset = all hosts):

```yaml
version: 3
install:
  name: spark-lab
  install_dir: ~/AI
hosts:
  - name: luna
    ssh: luna.tail9d5411.ts.net
    remote: true
    monitoring: {instance_label: luna}
  - name: sol
    ssh: sol.tail9d5411.ts.net
    remote: true
    monitoring: {instance_label: adam-spark}
models:
  qwen38-27b:
    active: true
    hosts: [luna, sol]          # where it is served -- this IS the scale
    host_overrides:
      sol: {litellm: {model_name: adam-spark-qwen3-8-27b}}   # per-host tuning
```

- **Remote vs local is automatic.** `remote: true` means "reach it over SSH
  (Fabric) when I'm not on it". Running `spark-lab status` on `luna` itself
  converges `luna` **locally** and `sol` remotely, from the very same file.
- **Anything in a host entry** besides `name`/`ssh`/`remote` is a per-host
  override (deep-merged: `monitoring:`, `images:`, `install:`, ...), and
  `models.<m>.host_overrides.<host>` tailors a model per host.
- **Model scaling**: `spark-lab model up qwen38-27b --hosts sol` adds sol to
  the model's `hosts:` and converges it; `model down ... --yes` removes hosts
  and stops the workloads. One active model per host (enforced).
- **State stays on the managed node** (in its spark-lab checkout), so
  node-local and remote operation share one source of truth.
- **Host bootstrap**: `spark-lab init --hosts sol --yes` idempotently prepares
  a node (tools, git checkout from `install.repo_url`, install dir, tailscale).
- Prerequisite for remote hosts: SSH key access (your key, or `identity_file`;
  `user@host` in `ssh:` works too).

Legacy v1/v2 configs (single node, optional `install.remote`) keep working
unchanged; `spark-lab migrate` rewrites them to v3.

Design: [ADR-0008](docs/adr/0008-multi-host-cluster-config.md).
Operations notes: [docs/REMOTE_OPERATOR_MODE.md](docs/REMOTE_OPERATOR_MODE.md).

## Command surface

```
spark-lab init [--yes]                     # create config.yaml + .env
spark-lab init --hosts a,b [--yes] [--all] # bootstrap the selected hosts (idempotent)
spark-lab status [--hosts a,b]
spark-lab apply [--hosts a,b] [--dry-run] [--diff] [--restart-model]
spark-lab model up <m> [--hosts a,b]       # scale a model up
spark-lab model down <m> --yes [--hosts]   # scale a model down (stops workloads)
spark-lab model stop --yes                 # stop now; config unchanged; next apply restarts
spark-lab teardown --yes [--purge]         # model + whole stack
spark-lab upgrade --yes                    # engine + sparkrun + images, then re-apply
spark-lab check [--images] [--probe] [--system] [--install] [--all]
spark-lab logs <service> [--hosts <one>] [-f]
spark-lab migrate [--dry-run]              # v1/v2 -> v3
spark-lab adopt [--dry-run]                # take over an existing install (state only)
spark-lab recipes {search,list,show,convert}
# hidden aliases: validate == check, doctor == check --system
```

## Layout

```
config.example.yaml     # v1/v2 single-node example
config.example.v3.yaml  # multi-host cluster example (hosts: + model hosts/host_overrides)
.env.example            # copy to .env (gitignored); secrets, generated by init
bin/spark-lab           # CLI entrypoint
sparklab/               # Python engine: config, render, converge, state, cluster, remote
templates/              # Jinja templates + static Grafana dashboards
docs/                   # SETUP, ARCHITECTURE, OPERATIONS, MODEL_RECIPES, NETWORKING, adr/
scripts/capture.sh      # capture read-only terminal output (for docs/blog)
```

## Docs

- [SETUP](docs/SETUP.md) — fresh install on one Spark, end to end.
- [ARCHITECTURE](docs/ARCHITECTURE.md) — the data flow and why each piece exists.
- [OPERATIONS](docs/OPERATIONS.md) — day-2: upgrading, monitoring, debugging.
- [MODEL_RECIPES](docs/MODEL_RECIPES.md) — how sparkrun recipes + SGLang tuning work.
- [NETWORKING](docs/NETWORKING.md) — Tailscale and optional Cloudflare.
- [REMOTE_OPERATOR_MODE](docs/REMOTE_OPERATOR_MODE.md) — operating Sparks remotely over SSH.

## Safety model

`apply` is safe by default: it starts/recreates services idempotently but will
**not** restart the running model unless you pass `--restart-model`.
`--dry-run` prints the plan and touches nothing. Secrets live only in the
gitignored `.env` and on the node — the repo contains no credentials.

### Converges to the declared state

`apply` is declarative: it renders your config, diffs against the last applied
state, and only acts on the difference — so **changing `config.yaml` (or pulling
newer templates) and re-running `apply` converges every selected host to the new
state**, including the running model:

- **Add / change** a recipe or service → detected and applied.
- **Switch or drop** a model → the old workload is stopped (gated) and the new
  one started; a removed service is reconciled via `docker compose up --remove-orphans`.
- **Files that no longer render are removed** (e.g. an old recipe file after a
  rename) — only after the model commands have run, so the stop step can still
  address the old recipe by path, and never while a restart is still gated.
- **Files-on-disk vs model-running are tracked separately.** A recipe change that
  hasn't been restarted stays *pending* and keeps prompting you to run
  `apply --restart-model` — it does **not** silently record the new recipe as applied.
- **No-op** re-runs are idempotent (no model restart).
- **Scale a model across hosts** with `model up` / `model down` (they edit the
  config's `models.<m>.hosts` and converge the affected hosts); stopping just
  the model without changing config: `model stop --yes` (the next routine
  `apply` re-starts it — converge semantics).
- **Split the observability stack** with `monitoring.role` per host:
  `full` (default: prometheus + grafana + exporters), `exporters` (exporter
  sidecars only — a `full` host's prometheus scrapes it remotely, so one
  central Grafana covers the whole cluster), or `none`.
- `spark-lab upgrade --yes` refreshes the engine deps, `sparkrun`, and the stack
  images per host, then re-applies with the model restart allowed.

The converge decisions are pinned by `tests/test_converge.py` (add / remove /
switch / no-op / pending), which runs in CI.

### Secret gate (no secrets, ever)

A `pre-commit` hook (`.githooks/pre-commit`) runs `scripts/secret-scan.sh` on
every commit and **blocks** it if a secret-looking value would be committed. It
scans only tracked/staged files, so the gitignored `.env`/`config.yaml` are never
flagged. Run it manually anytime with `scripts/secret-scan.sh` (install
`gitleaks` for broader coverage; the built-in grep scan is the fallback).

The full rule — including that `--no-verify` is off-limits — is in
[AGENTS.md](AGENTS.md), which every agent working in this repo loads.

## License

MIT — see [LICENSE](LICENSE).
