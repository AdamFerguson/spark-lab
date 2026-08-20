# spark-lab — Design Contract (SPEC)

> This file is the **single source of truth** for the kit and the blog post.
> Both implementers build against it. If you must deviate, record the deviation
> in this file's "Deviations" section. `config.example.yaml` is the authoritative
> schema for the user-facing config file.

## 1. Goals
- Anyone with a DGX Spark (or a cluster of Sparks managed by `sparkrun`) can
  self-host an OpenAI-compatible LLM endpoint, monitor it, and optionally expose
  it — from **one config file + one command**.
- **Idempotent + convergent:** re-running `spark-lab apply` after any change to
  `config.yaml`, `config` templates, or the litellm stack brings the node(s) to
  match config. No manual drift-chasing.
- **Portable & clean:** the public repo contains zero personal names, hostnames,
  IPs, or secrets. Every identifying value is either a config knob or an env var.

## 2. Fixed layout (exact paths)
```
spark-lab/
  README.md
  LICENSE                        # MIT
  .gitignore                     # ignore: .env, config.yaml, deploy/, .venv/, .sparklab-state/
  config.example.yaml            # (already present — DO NOT restructure the schema)
  .env.example                   # secret knobs; .env is gitignored
  SPEC.md                        # this file
  bin/spark-lab                  # bash entrypoint -> execs managed venv python
  lib/
    __init__.py
    cli.py                       # arg parsing + command dispatch
    config.py                    # load+validate config.yaml, resolve env refs
    render.py                    # Jinja render templates/ -> deploy/
    converge.py                  # diff deploy/ vs live, run the right commands
    state.py                     # .sparklab-state/*.sha256 read/write
  templates/
    sparkrun_recipe.yaml.j2      # -> <install_dir>/sparkrun/recipes/<recipe_name>.yaml
    docker-compose.yaml.j2       # -> <install_dir>/litellm/docker-compose.yml
    litellm_config.yaml.j2       # -> <install_dir>/litellm/config.yaml
    litellm_model_config.yaml.j2 # -> <install_dir>/litellm/model_config.yaml
    litellm.env.j2               # -> <install_dir>/litellm/.env
    prometheus.yml.j2            # -> <install_dir>/litellm/prometheus.yml
    grafana/provisioning/datasources/prometheus.yml.j2
    grafana/provisioning/dashboards/dashboards.yml.j2
    grafana/dashboards/sglang-dashboard.json.j2
    grafana/dashboards/spark-host-overview.json.j2
    scripts/nvidia-gpu-textfile.sh.j2
  docs/
    SETUP.md  ARCHITECTURE.md  OPERATIONS.md  MODEL_RECIPES.md  NETWORKING.md
  scripts/
    capture.sh                   # capture read-only terminal output for the blog
  .github/workflows/validate.yml # CI: render w/ config.example.yaml, compose config, lint
```

## 3. Config schema
Authoritative in `config.example.yaml`. Sections: `install`, `model`, `litellm`,
`monitoring`, `network`. Key rules:
- **Secrets are env-var *names*, not values.** e.g. `model.hf_token_env: HF_TOKEN`
  means "read `$HF_TOKEN` from `.env`". `init` writes `.env` from `.env.example`
  and generates keys with `openssl rand -hex 32`.
- `install.hosts` drives single-node vs cluster. One host → local; >1 → cluster.
- `model.params` maps 1:1 to SGLang serve flags; `extra_flags` appended verbatim.
- `litellm.db.*`, `redis`, `monitoring.*`, `network.*` gate which services render
  into the compose file.

## 4. CLI contract (`bin/spark-lab`)
Bash wrapper: ensures a managed venv (prefer `uv venv`+`uv pip install pyyaml
jinja2`; fallback `python3 -m venv`+`pip`), then `exec .venv/bin/python -m lib.cli "$@"`.
Commands (Typer-style or argparse — implementer's choice, but keep these verbs):

| Command | Behavior |
|---|---|
| `init` | Create `config.yaml` (from example if absent) + `.env` (from example, generate keys). Interactive prompts only for missing secrets; respect `--yes` to use defaults. |
| `apply [--dry-run] [--apply] [--hosts a,b | --cluster name]` | **Converge.** Render → deploy/; write to target dirs; diff vs state; act (see §5). Destructive actions (sparkrun restart, compose recreate) require `--apply` or an explicit `--yes`; `--dry-run` prints the plan only. |
| `status` | `sparkrun status` + `docker compose -f <install_dir>/litellm/docker-compose.yml ps` + `tailscale status` + `cloudflared` presence; pretty table. |
| `teardown [--yes] [--purge]` | `sparkrun stop --all` (scoped to our labels) + `docker compose down`; `--purge` also removes named volumes. |
| `upgrade` | `sparkrun update`; `docker compose pull`; re-`apply`. |

Global flags: `--config PATH` (default `./config.yaml`), `-v` verbose, `--json`
(machine-readable status). Every command that mutates must be safe under
`--dry-run`.

## 5. Converge algorithm (`lib/converge.py`)
1. `render` templates → `deploy/` (mirror the target tree, using `.j2` inputs +
   `config.yaml` + resolved `.env`).
2. Sync `deploy/` → target dirs (`<install_dir>/sparkrun/recipes/…`,
   `<install_dir>/litellm/…`). `install_dir` may be `~`-prefixed → expanduser.
3. **State + diff.** For each target file store sha256 in
   `.sparklab-state/<relative>.sha256`. Classify changed/added/removed vs last apply.
4. **Actions, only for what changed:**
   - litellm stack files changed → `docker compose up -d` (compose recreates only
     the changed services; volumes persist).
   - recipe file changed → `sparkrun stop <recipe>` then `sparkrun run <recipe>
     --ensure`. Unchanged → `sparkrun run <recipe> --ensure` (no-op if up).
   - cloudflare enabled and its rendered config changed → `systemctl reload
     cloudflared`.
   - tailscale enabled → `systemctl enable --now tailscaled` (idempotent).
   - multi-node: `sparkrun setup ssh --hosts …`, `sparkrun cluster
     create/update`, `sparkrun run <recipe> --cluster <name>`.
5. Write new hashes to state. Print a per-node summary (what changed / no-op).

## 6. Target install layout on a node
```
<install_dir>/
  sparkrun/recipes/<model.recipe_name>.yaml
  litellm/
    docker-compose.yml  config.yaml  model_config.yaml  .env  prometheus.yml
    grafana/provisioning/datasources/prometheus.yml
    grafana/provisioning/dashboards/dashboards.yml
    grafana/dashboards/{sglang-dashboard.json,spark-host-overview.json}
    scripts/nvidia-gpu-textfile.sh
```
`apply` (re)creates `<install_dir>`; it never deletes user data not owned by the
kit (teardown is the only destructive path, and it needs `--yes`).

## 7. Naming / ports reference
| Thing | Default | Where |
|---|---|---|
| SGLang serve port | 30000 | `model.port` |
| LiteLLM gateway | 4000 | `litellm.port` |
| Postgres | 5432 (internal) | compose `db` |
| Redis | 6379 | `litellm.redis.port` |
| Prometheus | 9090 | `monitoring.prometheus.port` |
| Grafana | 3000 | `monitoring.grafana.port` |
| node_exporter / DCGM / cAdvisor | 9100 / 9835 / 8080 | compose |
| Prometheus `external_labels` | `hardware: gb10`, `host: <instance_label>` | templated |

## 8. Sanitization rules (repo must contain NONE of these)
- Personal names, hostnames (`your-spark`/`spark.local` placeholders OK), IPs
  other than `127.0.0.1`, usernames, domains.
- Any key/token/password. Secrets = env-var names in config + `.env` (gitignored).
  `.env.example` uses obvious placeholders (`sk-CHANGE_ME`, `CHANGE_ME`).
- Grafana dashboard JSONs: no host-specific labels (e.g. `instance="<your-host>"`);
  use the templated `{{ instance_label }}`.

## 9. Template notes
- SGLang recipe: generalize your `qwen-3-8-27b-dspark-nvfp4.yaml`. Keep
  `executor: docker` + the Blackwell workarounds (`privileged: true`,
  `cap_add: [SYS_PTRACE]`, `security_opt: [seccomp=unconfined]`, `ipc: host`,
  `shm_size: 32g`) — they're required on GB10. Model/image from `model.*`.
- litellm compose: services `litellm, db, redis (if enabled), prometheus (if
  monitoring.enabled), grafana, node_exporter, dcgm_exporter, cadvisor,
  gpu_textfile`. Use the **GB10 node_exporter tweaks** (`--no-collector.hwmon`,
  `--no-collector.cpufreq`, textfile dir) and the `nvidia-gpu-textfile.sh`
  sidecar (from `cadaverine/dgx-spark-observability`). Grafana admin password
  from env; default home dashboard = `sglang-dashboard.json`.
- prometheus.yml: jobs `prometheus, sglang (host.docker.internal:30000, with
  `sglang:` → `sglang_` metric_relabel), node, dcgm, cadvisor`. Scrape timeouts
  generous on GB10.
- litellm_model_config.yaml: `model_list[0]` = `custom_openai/<model>` with
  `api_base: http://<instance>:<model.port>/v1`, `api_key: not-needed`,
  `model_info` from `litellm.model_info`, `litellm_settings` (temperature/top_p/
  top_k) + reasoning-effort support from config.

## 10. Verification checklist (implementer A)
- [ ] `bin/spark-lab apply --dry-run` (against `config.example.yaml`) renders all
      templates without error and prints a plan.
- [ ] Rendered `docker-compose.yml` passes `docker compose config -q`.
- [ ] Rendered recipe + all YAML pass a YAML parse; `shellcheck` clean on `.sh`.
- [ ] Grep audit: no personal names, hostnames, real IPs, real keys/tokens/
      passwords, or personal email anywhere in the repo.
- [ ] `LICENSE` = MIT; `.gitignore` correct; `README.md` has a 30-sec quickstart.

## 11. Blog post (implementer B)
- File: your Astro blog's content collection, e.g. `src/content/blog/<slug>.md`
  (one Markdown file per post; the filename becomes the URL slug).
  Frontmatter (per the blog's content schema): `title, description, date (today),
  tags: [llm, infrastructure, self-hosting, gpu], category: Infrastructure, draft: true`.
- **Voice (from `DESIGN.md`):** overreacted.io + anthropic.com — warm, editorial,
  prose-first, quiet chrome, minimal, human. NOT neon/tech-futurist, no hype.
- **Narrative beats:** (1) the itch — running a real model on a quiet desktop
  box; (2) the stack in plain terms (Spark → SGLang → LiteLLM gateway →
  Prometheus/Grafana → Tailscale / optional Cloudflare); (3) the payoff: one
  config file + `spark-lab apply`, and it *converges* when you change things;
  (4) what you get (a Grafana you can actually read, private access, optional
  public share); (5) "run it on your own Spark" with a link to the repo.
- **Images:** (a) a clean inline-SVG **architecture diagram** in the post or
  `public/`; (b) a few **read-only terminal snippets** captured via
  `spark-lab/scripts/capture.sh` (or direct ssh `sparkrun status`,
  `docker compose ps`, `nvidia-smi`) — sanitized; (c) clearly-marked
  `<!-- SCREENSHOT: … -->` placeholder slots for the owner's own Grafana/UI shots.
- Link to the `spark-lab` repo for the hands-on path.

## 12. Deviations
(none yet)
