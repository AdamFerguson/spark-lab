"""`spark-lab status` — the live view of the cluster, per selected host.

Human mode: the placement table, then per host the raw `sparkrun status`,
`compose ps`, the LIVE engine inventory (containers answering /v1/models --
managed or hand-started) and the gateway's actually-served model list, then
tailscale. `--json` emits one machine-readable object (placement + per-host
inventory) for scripts and for `sync`.
"""
from __future__ import annotations

import json
import sys

from ..core import cluster, config, converge, inventory
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

    print("\n== engines (live: anything answering /v1/models) ==")
    inv = inventory.discover(runtime, cfg)
    if not inv["engines"]:
        print("   (none found)")
    for e in inv["engines"]:
        print(f"   {e['container']:<28} :{e['port']:<6} {', '.join(e['models'])}")
    if inv["gateway"] is not None:
        gw = inv["gateway"]
        if gw["reachable"]:
            print(f"\n== gateway (:{gw['port']}) serves ==")
            for name in gw["served"]:
                print(f"   {name}")
        else:
            print(f"\n== gateway (:{gw['port']}) NOT reachable ==")

    print("\n== tailscale ==")
    run_command(["tailscale", "status"], ok=True, runtime=runtime)
    if cfg.cloudflare().get("enabled", False):
        print("\n== cloudflare ==")
        run_command(["systemctl", "is-active", "cloudflared"], runtime=runtime)
    return 0


def _placement_rows(cfg):
    return [{"host": h, "model": m, "gateway_name": n, "control_plane": cp}
            for h, m, n, cp in cfg.placement_table()]


def _json_status(cfg, ts) -> int:
    hosts = {}
    for t in ts:
        inv = inventory.discover(t.runtime, t.cfg)
        hosts[t.name] = inv
    print(json.dumps({"config": str(cfg.config_path),
                      "placement": _placement_rows(cfg),
                      "hosts": hosts}, indent=2, sort_keys=True))
    return 0


def run(args) -> int:
    cfg = config.load(args.config)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    if getattr(args, "json", False):
        return _json_status(cfg, ts)
    rows = _placement_rows(cfg)
    if rows:
        print("== placement (from config) ==")
        print(f"{'host':<8} {'model':<32} {'gateway name':<24} control plane")
        for r in rows:
            print(f"{r['host']:<8} {r['model'] or '(none)':<32} "
                  f"{r['gateway_name'] or '-':<24} {'on' if r['control_plane'] else 'off'}")
        print()
    return cluster.run_on_each(ts, _status_one)
