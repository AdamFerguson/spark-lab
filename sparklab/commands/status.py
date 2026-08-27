"""`spark-lab status` — show workloads, stack, network status (per selected host)."""
from __future__ import annotations

from ..core import cluster, config, converge
from ..util import run_command


def _status_one(t) -> int:
    cfg, runtime = t.cfg, t.runtime
    sparkrun = converge.find_sparkrun(runtime)
    home = runtime.home_path() if runtime is not None else None
    compose_file = cfg.node_path("litellm/docker-compose.yml", home)
    print("== sparkrun ==")
    run_command([sparkrun, "status"], ok=True, runtime=runtime)
    print("\n== docker compose ==")
    run_command(["docker", "compose", "-f", compose_file, "ps"], ok=True, runtime=runtime)
    print("\n== tailscale ==")
    run_command(["tailscale", "status"], ok=True, runtime=runtime)
    if cfg.cloudflare().get("enabled", False):
        print("\n== cloudflare ==")
        run_command(["systemctl", "is-active", "cloudflared"], ok=True, runtime=runtime)
    return 0


def run(args) -> int:
    cfg = config.load(args.config)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    return cluster.run_on_each(ts, _status_one)
