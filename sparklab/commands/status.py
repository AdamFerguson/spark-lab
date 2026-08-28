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


def _placement_table(cfg) -> int:
    """Print the derived host -> model table (once, from the cluster config).

    The source of truth remains ``models.<m>.hosts``; this is a read-only
    inverse view (ADR-0009) so 'which model ends up where' is always visible.
    """
    rows = cfg.placement_table()
    if not rows:
        return 0
    print("== placement (from config) ==")
    print(f"{'host':<8} {'model':<32} {'gateway name':<24} control plane")
    for host, alias, name, cp in rows:
        m = alias or "(none)"
        n = name or "-"
        print(f"{host:<8} {m:<32} {n:<24} {'on' if cp else 'off'}")
    print()
    return 0


def run(args) -> int:
    cfg = config.load(args.config)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    _placement_table(cfg)
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    return cluster.run_on_each(ts, _status_one)
