"""`spark-lab swap` -- look at / steer the model zoo (llama-swap, ADR-0010).

`swap status`            -- which zoo models are resident right now, per host
`swap unload [model] -y` -- force-unload one model (or all, with no argument)

Daily use needs NEITHER: requesting a zoo model through the gateway loads it
automatically, and TTLs unload idle ones. These are for pinning RAM and
debugging.
"""

from __future__ import annotations

import shlex
import sys

from ..core import cluster, config as config_mod, inventory


def _status_one(t) -> int:
    cfg, runtime = t.cfg, t.runtime
    port = cfg.swap_port()
    r = runtime.run_capture(["sh", "-c", f"curl -sf -m 3 http://127.0.0.1:{port}/running || true"])
    resident = inventory.swap_resident(r.stdout or "")
    reachable = r.returncode == 0 and bool((r.stdout or "").strip())
    print(f"   {t.name} (:{port}, zoo: {', '.join(cfg.swap_aliases())})")
    if not reachable:
        print("      llama-swap not reachable -- run `spark-lab zoo prepare`?")
    elif not resident:
        print("      resident: (none -- everything idle/unloaded)")
    else:
        for m in resident:
            print(f"      resident: {m}")
    return 0


def _unload_one(t, model) -> int:
    cfg, runtime = t.cfg, t.runtime
    endpoint = "/api/models/unload" + ("/" + shlex.quote(str(model)) if model else "")
    r = runtime.run_capture(
        ["sh", "-c", f"curl -sf -m 10 -X POST http://127.0.0.1:{cfg.swap_port()}{endpoint} || true"]
    )
    what = model or "ALL models"
    if r.stdout and r.returncode == 0:
        print(f"   {t.name}: unload requested for {what}")
        return 0
    print(f"   {t.name}: unload of {what} FAILED (daemon down, or model not running?)", file=sys.stderr)
    return 1


def run(args) -> int:
    cfg = config_mod.load(args.config)
    if not cfg.swap_enabled():
        print("swap.enabled is false -- the zoo is not active.", file=sys.stderr)
        return 1
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = [t for t in cluster.targets(cfg, names, runtime=getattr(args, "runtime", None)) if t.cfg.swap_for_host()]
    if not ts:
        print("no swap-enabled models placed on the selected hosts", file=sys.stderr)
        return 1
    verb = getattr(args, "swap_cmd", "status")
    if verb == "status":
        print("== spark-lab swap status ==")
        return cluster.run_on_each(ts, _status_one)
    model = getattr(args, "model", None)
    if model is None and not getattr(args, "yes", False):
        print(
            "unloading ALL zoo models requires --yes (a pin survives; TTL unloads are automatic anyway).",
            file=sys.stderr,
        )
        return 1
    if model is not None and model not in cfg._swap_defs():
        print(f"'{model}' is not a swap model (zoo: {', '.join(cfg._swap_defs()) or 'none'})", file=sys.stderr)
        return 1
    print(f"== spark-lab swap unload {'<all>' if model is None else model} ==")
    return cluster.run_on_each(ts, lambda t: _unload_one(t, model))
