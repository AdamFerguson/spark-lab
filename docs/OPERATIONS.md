# Operations (day-2)

> **Multi-host:** every command takes `--hosts a,b` (unset = all hosts) when the
> config is schema v3 (ADR-0008). The same `config.yaml` runs on the laptop and
> on every Spark — a host you are standing on is converged locally, the rest
> over SSH.
>
> **First `apply` is slow + blocking.** On a cold node it pulls the model image
> (many GB) + downloads the weights + waits for the engine to warm up -- several
> minutes, and it blocks until the model is actually up. Run it from a persistent
> terminal (`tmux`/`screen`), not a flaky SSH session: if the session dies mid-run,
> the model keeps running (sparkrun detaches it) but the rest of the converge is
> undone -- just re-run `spark-lab apply` (the model step is a no-op once it's up).

## Everyday

```bash
./bin/spark-lab status                 # workloads + stack + tailscale, per selected host
./bin/spark-lab status --hosts sol     # just one host
sparkrun logs <job-id>                 # follow the engine's logs (on the node; id from `sparkrun status`)
./bin/spark-lab logs litellm -f --hosts luna   # stack service logs through spark-lab (one host)
docker compose -f <install_dir>/litellm/docker-compose.yml logs -f litellm
./bin/spark-lab model stop --yes       # stop the model workload now (stack stays up; next apply restarts)
./bin/spark-lab model up  qwen38-27b --hosts sol    # scale a model up onto a host
./bin/spark-lab model down qwen38-27b --yes --hosts luna   # scale it off a host (stops it there)
```

`model stop` is gated like `teardown` and records the stop in state; the next
routine `apply` starts the model again (`sparkrun run --ensure`). `model up` /
`model down` edit the config's `models.<m>.hosts` and converge the affected
hosts — that list *is* the scale. `model down` stops the workload on the
dropped host and leaves the old recipe file there, unmanaged (inert until a
re-scale-up re-renders it).

## Changing something

1. Edit the cluster `config.yaml` (or a `templates/` file if you're customizing
   the stack). For per-host differences, use the host entry's overrides or
   `models.<m>.host_overrides.<host>` — not a separate config per host.
2. `./bin/spark-lab apply --dry-run` — confirm the diff, per host.
3. `./bin/spark-lab apply` — recreate the LiteLLM stack where it changed.
   If the model **recipe** changed, add `--restart-model`:
   `./bin/spark-lab apply --restart-model`.

Because `apply` converges on the file hashes, a no-op run is cheap and safe to
re-run any time (e.g. after a reboot).

## Boot survival

`spark-lab check` probes it per host: containers with restart policy `no`
(would stay down after a reboot) and services not enabled at boot. The
rendered compose pins `restart: always` on the gateway trio (litellm/db/redis)
and every monitoring service, so `apply`ing once makes the stack reboot-proof.
**Engines you start by hand are yours**: launch them with
`--restart unless-stopped` (or `docker update --restart unless-stopped <name>`)
or they will not survive a reboot — `check` warns about any such container
(e.g. a manual `vllm-fn`).

## Adding a new Spark to the cluster

```bash
# prepare the node yourself: docker + tailscale + `uv tool install sparkrun`
# (+ a spark-lab checkout at install.repo_dir for node-side state), then:
# add the node under `hosts:` in config.yaml
./bin/spark-lab check --hosts <new-host>           # binaries + render pre-flight
./bin/spark-lab apply --hosts <new-host>           # converge it (first run: big pulls)
```

## Upgrades

```bash
# per host (ssh or via tailscale):
uv tool upgrade sparkrun                # engine deps + sparkrun CLI
cd ~/AI && docker compose -f litellm/docker-compose.yml pull
./bin/spark-lab apply                   # re-converge (+ --restart-model if a recipe changed)
```

Pinning versions: edit `model.image`, `litellm`/`monitoring` image tags in
`config.yaml` to exact tags (instead of `main-stable`/`latest`) for reproducible
upgrades — per-host pins via the host entry's `images:` overrides.

## Model zoo (swap without thinking)

Want to try a new model without a manual stop/start dance? Add it as a **zoo
model** (`swap.enabled: true` under `swap:` + an `active: false` model with a
`swap:` block — see `config.example.yaml`). Requesting it through the gateway
loads it; idle models unload (`swap.ttl`) so the other Spark's RAM frees up
automatically. Full design: [ADR-0010](adr/0010-zoo-model-swapping.md).

```bash
# one-time per zoo host: install the llama-swap binary (deliberate, not fetched
# by spark-lab). GB10 = linux_arm64, pinned v252:
ssh sol 'mkdir -p ~/AI/bin && cd /tmp && curl -sSLO \
  https://github.com/mostlygeek/llama-swap/releases/download/v252/llama-swap_252_linux_arm64.tar.gz \
  && tar xzf llama-swap_252_linux_arm64.tar.gz \
  && install -m755 llama-swap ~/AI/bin/llama-swap'

./bin/spark-lab apply --hosts sol   # render zoo config + unit (no daemon yet)
./bin/spark-lab zoo prepare --hosts sol   # install + start the llama-swap service
./bin/spark-lab swap status         # what is resident right now
```

Kits (Mia-AiLab-style `start.sh`/`stop.sh` repos, incl. multi-node ones) join
as **script-mode** zoo models: `zoo import --kit <dir> --host <head>` prints
the block, `zoo prepare` preflights the kit contract, and llama-swap drives
the kit's own scripts (pinned by default -- kit cold starts are long; unload
explicitly with `swap unload`).

Then just ask the gateway for a zoo model by name (or its `swap.aliases`);
the first request blocks while the engine loads (llama-swap streams a
"loading" state), and it stays up until its TTL elapses. `swap unload <model>
--yes` reclaims its RAM on demand; `unload --yes` clears the whole zoo.

Notes:
- **Resource safety (learned the hard way):** llama-swap can only reclaim
  engines *it owns*. Hand-started residents (a manual flash-next span, for
  instance) are NOT unloaded on swap — requesting a zoo model while one holds
  the node can OOM both the zoo load and the resident engine (earlyoom
  preferentially kills vllm/sglang). Only pilot the zoo on a quiet cluster,
  or bring the big spans under spark-lab management first (ADR-0010 phase 3)
  so llama-swap can unload them too.
- The llama-swap port (default `9292`) is LAN/tailnet-only — never expose it;
  the gateway (port 4000) stays the only public/authenticated surface.
- Zoo models are `active: false` — `apply` never launches them and
  `model up/down` refuse them (llama-swap owns the lifecycle via sparkrun).
- First `zoo prepare` needs the binary present (the command prints the exact
  install line if it is missing). The user service persists across reboots
  only if lingering is enabled (`zoo prepare` enables it, prompting for sudo).
- `swap.fast_resume` (seconds-scale suspend/restore via vllm-snapshot) is a
  planned opt-in per model — not yet implemented.

## Monitoring

- **Grafana** — `http://<spark>:3000` (over Tailscale, use the mesh name).
  Default home dashboard: SGLang. Also: host overview.
- **Prometheus** — `http://<spark>:9090` for raw metrics / query editor.
- Quick health from the shell:
  ```bash
  curl -s http://localhost:30000/health            # SGLang
  curl -s http://localhost:4000/health/liveliness  # LiteLLM
  ```

## Debugging

| Symptom | Where to look |
|---|---|
| Model won't start | `sparkrun logs <id>`; check `mem_fraction_static` fits the model in unified memory. |
| Gateway 502 | `docker compose ps`; SGLang may still be loading — retry. |
| Grafana empty | Is Prometheus scraping? `http://<spark>:9090/targets`; check `instance` label matches. |
| Dashboards show no series | Confirm SGLang started with `--enable-metrics` (in `model.extra_flags`). |
| 401 from gateway | Client isn't sending `Authorization: Bearer <LITELLM_MASTER_KEY>`. |

## Rebuilding from scratch

> **Volumes:** a plain `teardown --yes` KEEPS all named volumes (the LiteLLM
> database, redis, prometheus/grafana data) — a re-apply restores the stack on
> the same data. `--purge` is the ONLY path that destroys them (it prints an
> explicit per-volume warning, naming the database as unrecoverable).

```bash
./bin/spark-lab teardown --yes --purge   # per selected host (--hosts to scope): stop model, down + remove volumes
rm -rf <install_dir>/litellm <install_dir>/sparkrun/recipes   # on each host
rm -rf .sparklab-state                    # reset converge state (node-side: ~/spark-lab/.sparklab-state remote)
./bin/spark-lab apply
```

## Multi-host operation (v3)

One `config.yaml` describes every managed node (the `hosts:` list); each node
keeps its state in its own spark-lab checkout
(`<repo_dir>/.sparklab-state/state.json`, default `~/spark-lab`). Day-2 notes:

- **Same file everywhere.** Use the identical cluster config on the laptop and
  on every Spark. A host you are standing on is auto-detected and converged
  **locally** (no SSH to yourself); the remaining hosts go over SSH. All-hosts
  commands *from a node* need that node to have ssh access to the other nodes;
  self-targeted commands never do.
- **PATH**: remote commands run in a login shell (`bash -lc`) with
  `~/.local/bin` + `~/.cargo/bin` prepended, so uv-installed tools (sparkrun,
  uv) resolve. If you install tools somewhere exotic, that's the place to look.
- **Long commands** (the bounded model-readiness probe can run up to ~10 min)
  execute inside one SSH session; if the node drops idle connections early,
  raise its `ClientAliveInterval` or shorten the probe.
- **Host roles.** Two per-host switches shape what runs where: `monitoring.role`
  (`full` / `exporters` / `none`) and `control_plane.enabled` (default `true`).
  An observability-only host — e.g. `monitoring: {role: exporters}` +
  `control_plane: {enabled: false}` — runs just the exporter sidecars, scraped
  by a `full` host's central prometheus. Disabling the control plane stops the
  gateway/db/redis containers and removes their config files, but the
  `litellm_postgres_data` / `litellm_redis_data` volumes stay on disk: flip the
  flag back to `true` and `apply --hosts <host>` restores the gateway on its
  previous database.
- **Central gateway (implicit serving).** Every active model is registered in
  the `model_list` of every control-plane host — no per-model declaration.
  The entry's `api_base` is the local engine when the model runs on that host,
  the running host's tailnet/LAN address otherwise. So "move the model between
  Sparks" behind one gateway is just changing `models.<m>.hosts`. Safe two-step
  move (zero client disruption until you say so):
  1. `hosts: [luna, sol]` + `apply --hosts luna` — starts the model on luna;
     sol keeps running its own copy, its entry stays local.
  2. `hosts: [luna]` + `apply --restart-model --hosts sol` — sol's entry flips
     to `http://luna:<port>/v1` (same model name; a best-effort litellm restart
     makes it take effect, a few seconds), then sol's model workload stops
     (its recipe file is left on sol, unmanaged).
  Reversal: `model up <m> --hosts sol` (start the local copy first) then
  `model down <m> --yes --hosts luna`.
  `check` refuses configs where active models would run with no
  control-plane host to serve them, or where two models share a serving name
  on one gateway.
- **Recipes as source of truth + placement pins (ADR 0009).** v3 model blocks
  reference `recipes/<name>.yaml` (plain sparkrun recipes — directly
  `sparkrun run`-able, no secrets, no layout) instead of declaring the launch
  inline; gateway metadata + the readiness bound live in the recipe's
  `metadata:` section, and the HF token env-var name in
  `models.<m>.hf_token_env`. `apply` renders a node-side copy that adds a
  `layout:` pin from `hosts:` (structural placement — the scheduler honors it
  verbatim) and the token. Migration note: adding the layout block changes
  the rendered recipe's content hash, so the one-time apply after migrating
  to the reference form offers a model restart that is **skippable** —
  `sparkrun run --ensure` matches the running workload by intent (layout is
  not part of it), so the live container is unaffected.
- **Qwen3.8-Flash-Next on luna (second model).** The 125B MoE + 51B PLE recipe
  (`models.qwen38-flash-next`, plain sparkrun recipe `recipes/qwen38-flash-next.yaml`)
  needs a one-time node-side preparation on luna before the first `apply` —
  the pinned SGLang image ships two source files that must be patched (PLE
  NVMe-mmap allocation + shard-reuse fast path; the QSA sm_121 decode gate),
  and sparkrun bind-mounts the patched files back into the image read-only:
  ```bash
  # on luna, from a repo checkout (idempotent; ~20 min for the image pull +
  # the ~135 GB pinned-revision weight download when cold):
  HF_TOKEN="$(sed -n 's/^HF_TOKEN=//p' ~/spark-lab/.env | tr -d '"')" \
    bash scripts/flash-next-prepare.sh
  ```
  It creates `~/AI/flash-next/{build,ple,sglang-cache}` — the build
  dir holds the patched `qwen4_exp.py` / `qwen_sparse_attn_backend.py` (and
  `in_image_paths.txt`, which the script cross-checks against the recipe's
  mount targets when the image tag ever changes layout). Then `apply` (or
  `model up qwen38-flash-next --hosts luna`) converges it: first boot fills
  the 48 GB PLE table, ~45-60 min of quiet (the config's
  `readiness_seconds: 5400` covers it; later boots are ~10 min thanks to the
  shard-reuse patch). The engine serves under its HF model id; sol's gateway
  exposes it as `qwen38-flash-next` at `http://luna:30000/v1` (implicit
  central serving). Client notes: thinking is on by default
  (`chat_template_kwargs: {"enable_thinking": false}` to turn it off;
  `reasoning_effort` does nothing on this build); vision is on; tool calls
  use the `qwen3_coder` parser. Scale it off later with
  `model down qwen38-flash-next --yes --hosts luna` (the PLE backing file and
  weights stay on luna's disk — delete them by hand if you want the ~180 GB
  back).
- **Config drift:** because one file is the source of truth, edit it in one
  place and copy it to the other machines (it's small and gitignorable — the
  `.env` next to it is the only secret-bearing file).
- **Prerequisite**: SSH key access to each remote host (any machine that will
  drive it). `spark-lab check` checks each target node's binaries remotely,
  so a missing tool on a *node* is reported on your machine.
- **Crash-looping prometheus/grafana** (`permission denied` on their config in
  the container logs): an early version of the tool wrote install files with
  0600 modes, unreadable by the containers' users. Converge now sets modes on
  write, but only *changed* files are re-written, so a pre-fix node needs a
  one-time fix: `chmod 644 <install_dir>/litellm/{prometheus.yml,config.yaml,
  model_config.yaml,docker-compose.yml} <install_dir>/litellm/grafana/...` and
  `docker restart litellm-prometheus-1 litellm-grafana-1`.

Design + full history: [ADR-0008](adr/0008-multi-host-cluster-config.md) and
[REMOTE_OPERATOR_MODE.md](REMOTE_OPERATOR_MODE.md) (the original per-node remote
mode; its mechanism is kept, its one-config-per-node layout is superseded).

## Telemetry / safety notes

- `sparkrun` collects anonymous telemetry by default; `sparkrun setup telemetry`
  to view/change.
- The SGLang container runs `--privileged` with `SYS_PTRACE` + `seccomp=unconfined`
  because of a known GB10/Blackwell issue in that SGLang build. This is
  intentional; treat the node as a machine dedicated to inference.
- Anything reachable through Cloudflare is **public** — only enable it for
  services you're comfortable exposing, and gate access with the LiteLLM key.
