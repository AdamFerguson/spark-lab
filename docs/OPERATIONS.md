# Operations (day-2)

> **First `apply` is slow + blocking.** On a cold node it pulls the model image
> (many GB) + downloads the weights + waits for the engine to warm up -- several
> minutes, and it blocks until the model is actually up. Run it from a persistent
> terminal (`tmux`/`screen`), not a flaky SSH session: if the session dies mid-run,
> the model keeps running (sparkrun detaches it) but the rest of the converge is
> undone -- just re-run `spark-lab apply` (the model step is a no-op once it's up).

## Everyday

```bash
./bin/spark-lab status                 # workloads + stack + tailscale at a glance
sparkrun logs <job-id>                 # follow the engine's logs (id from `sparkrun status`)
docker compose -f <install_dir>/litellm/docker-compose.yml logs -f litellm
```

## Changing something

1. Edit `config.yaml` (or a `templates/` file if you're customizing the stack).
2. `./bin/spark-lab apply --dry-run` — confirm the diff.
3. `./bin/spark-lab apply` — recreate the LiteLLM stack if it changed.
   If the model **recipe** changed, add `--apply` to restart the model:
   `./bin/spark-lab apply --apply`.

Because `apply` converges on the file hashes, a no-op run is cheap and safe to
re-run any time (e.g. after a reboot).

## Upgrades

```bash
./bin/spark-lab upgrade      # sparkrun update + docker compose pull + re-apply
sparkrun --version           # check the orchestrator version
```

Pinning versions: edit `model.image`, `litellm`/`monitoring` image tags in
`config.yaml` to exact tags (instead of `main-stable`/`latest`) for reproducible
upgrades.

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

```bash
./bin/spark-lab teardown --yes --purge   # stop model, down + remove volumes
rm -rf <install_dir>/litellm <install_dir>/sparkrun/recipes
rm -rf .sparklab-state                    # reset converge state
./bin/spark-lab apply
```

## Telemetry / safety notes

- `sparkrun` collects anonymous telemetry by default; `sparkrun setup telemetry`
  to view/change.
- The SGLang container runs `--privileged` with `SYS_PTRACE` + `seccomp=unconfined`
  because of a known GB10/Blackwell issue in that SGLang build. This is
  intentional; treat the node as a machine dedicated to inference.
- Anything reachable through Cloudflare is **public** — only enable it for
  services you're comfortable exposing, and gate access with the LiteLLM key.
