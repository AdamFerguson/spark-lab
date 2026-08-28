"""`spark-lab validate` — pre-flight, read-only (per selected host).

Confirms the config is usable BEFORE touching any node: schema + secrets
resolve, the host set is sane, every host's view renders, and required
binaries are present **on each host**. Writes nothing and runs no commands.
In remote mode the binary pre-flight checks each *target node*, not this
machine.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..core import cluster, config, render


def _validate_host(t, bins: list) -> int:
    """The per-host half of validation: binaries on that node + render its view."""
    cfg, runtime = t.cfg, t.runtime
    missing = []
    for b in bins:
        found = runtime.available(b) if runtime is not None else False
        if not found:
            missing.append(b)
        print(f"   bin {b:<12} {'ok' if found else 'MISSING (will be skipped at runtime)'}")
    try:
        rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-validate-")))
    except Exception as e:  # noqa: BLE001 - any render failure == invalid config
        print(f"[RENDER ERROR] {e}", file=sys.stderr)
        return 1
    where = (f"{cfg.install_dir_raw} on the node" if t.is_remote else str(cfg.install_dir))
    print(f"   render: OK ({len(rendered)} file(s) would be written to {where})")
    if missing:
        print(f"   WARN: missing binaries: {', '.join(missing)} — they'll be skipped at runtime.")
    return 0


def run(args) -> int:
    try:
        cfg = config.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1

    if cfg.is_v3:
        if cfg.active_host_conflicts():
            print(f"[INVALID] two active models share a host: {cfg._conflict_pairs()}",
                  file=sys.stderr)
            return 1
        for problem in cfg.control_plane_conflicts():
            print(f"[INVALID] {problem}", file=sys.stderr)
            return 1
        for problem in cfg.serving_conflicts():
            print(f"[INVALID] {problem}", file=sys.stderr)
            return 1

    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    try:
        ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    except ValueError as e:
        print(f"[INVALID] {e}", file=sys.stderr)
        return 1
    if not ts:
        print("[INVALID] no hosts in config (add a `hosts:` list for v3).", file=sys.stderr)
        return 1

    bins = ["sparkrun", "docker"]
    if cfg.tailscale().get("enabled", True):
        bins.append("tailscale")
    if cfg.cloudflare().get("enabled", False):
        bins.append("cloudflared")

    label = getattr(args, "_label", "validate")
    print(f"== spark-lab {label} ({cfg.config_path}) ==")
    print(f"  hosts: {', '.join(t.name for t in ts)}")
    rc = cluster.run_on_each(ts, lambda t: _validate_host(t, bins))
    if rc == 0:
        print("\nConfig is valid and renderable on every selected host. "
              "`spark-lab apply` is safe to run.")
    return rc
