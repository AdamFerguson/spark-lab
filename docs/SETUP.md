# Setup

Fresh install of spark-lab on a single DGX Spark, end to end. Assumes Ubuntu
24.04 on the Spark (the stock DGX OS) and that you can SSH to it.

## Prerequisites on the node

> **Quick check:** `./bin/spark-lab doctor` (a.k.a. `check system`) detects every
> required + optional tool, explains why each is needed, and with `--install`
> installs the missing ones. `./bin/spark-lab init` runs this check first.

- **uv** — the dependency/venv manager; it manages the spark-lab Python env and
  installs sparkrun. Install once:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **sparkrun** — the critical orchestrator. Install via `uv`:
  ```bash
  uv tool install sparkrun
  ```
  `spark-lab` finds it on `PATH` or at `~/.local/bin/sparkrun` (override with
  `SPARKRUN=/path/to/sparkrun` if it lives elsewhere).
- **Docker** — runs the LiteLLM gateway + monitoring stack (ships with the DGX
  Spark image). Verify: `docker ps`.
- **Tailscale** (optional but recommended) —
  `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`.

> `bin/spark-lab` manages the Python env via **uv** (`uv sync` from
> `pyproject.toml` + `uv.lock`; falls back to `pip install -e .` if uv is
> absent). There is no `requirements.txt` anymore.

## 1. Clone and initialize

```bash
git clone <this-repo> spark-lab
cd spark-lab
git config core.hooksPath .githooks   # install the pre-commit secret gate (once)
./bin/spark-lab init
```

> **No secrets, ever.** The pre-commit hook (`.githooks/pre-commit`) runs
> `scripts/secret-scan.sh` on every commit and blocks any that would introduce
> a secret. See [AGENTS.md](../AGENTS.md) for the full rule and the
> `git config core.hooksPath .githooks` step for fresh clones.

`init` creates `config.yaml` (from `config.example.yaml`) and `.env`
(generating the LiteLLM master/salt keys, DB password, and Grafana password).
Fill in what's left:

- `config.yaml` → point `model.hf_model` and `model.image` at the model you
  want to serve; set `litellm.model_name`, ports, dashboards, and network.
- `.env` → set `HF_TOKEN` if the model is gated; `CF_TUNNEL_TOKEN` if you'll
  use Cloudflare.

> The `params` block in `config.yaml` is a working example for a ~27B NVFP4
> model with DSPARK speculative decoding + fp8 KV cache. Adjust it (or swap the
> whole `model` section) for your model — see [MODEL_RECIPES](MODEL_RECIPES.md).

## 2. Preview, then apply

```bash
./bin/spark-lab apply --dry-run   # show exactly what will change + which commands run
./bin/spark-lab apply             # write the stack and start everything
./bin/spark-lab apply --apply     # also restart the model if the recipe changed
```

`apply` writes the SGLang recipe to `<install_dir>/sparkrun/recipes/` and the
LiteLLM + monitoring stack to `<install_dir>/litellm/`, then:

- `sparkrun run <recipe> --ensure` — start the model (no-op if already up).
- `docker compose up -d` — start LiteLLM, Postgres, Redis, and the monitoring stack.
- ensure `tailscaled` is running.

## 3. Verify

```bash
./bin/spark-lab status
# the gateway should answer:
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Grafana is at `http://localhost:3000` (admin / your `GRAFANA_ADMIN_PASSWORD`);
the SGLang dashboard is the default home dashboard.

## 4. Use it

Point any OpenAI-compatible client at `http://<spark>:4000/v1` with your
`LITELLM_MASTER_KEY` and model name `litellm.model_name`. Over Tailscale, use
the Spark's Tailscale hostname/IP.

## Uninstall

```bash
./bin/spark-lab teardown --yes        # stop model + remove containers (keeps volumes)
./bin/spark-lab teardown --yes --purge  # also remove named volumes (data loss)
rm -rf <install_dir>/litellm <install_dir>/sparkrun/recipes
```
