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
hosts — that list *is* the scale.

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

## Adding a new Spark to the cluster

```bash
./bin/spark-lab init --hosts <new-host>            # report: what would be prepared
./bin/spark-lab init --hosts <new-host> --yes      # tools + git checkout + install dir + tailscale
# add the node under `hosts:` in config.yaml (repo_url for the clone, if any)
./bin/spark-lab apply --hosts <new-host>           # converge it (first run: big pulls)
```

## Upgrades

```bash
./bin/spark-lab upgrade --yes      # per host: spark-lab deps + sparkrun update + pull + re-apply
sparkrun --version                 # check the orchestrator version (on the node)
```

Pinning versions: edit `model.image`, `litellm`/`monitoring` image tags in
`config.yaml` to exact tags (instead of `main-stable`/`latest`) for reproducible
upgrades — per-host pins via the host entry's `images:` overrides.

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
  previous database. `check`/`validate` refuse configs where a model-serving
  host has the control plane off, or where a host would run nothing.
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
