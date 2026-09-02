"""`spark-lab check` — pre-flight, read-only (per selected host).

Confirms the config is usable BEFORE touching any node: schema + secrets
resolve, the host set is sane, every host's view renders, and required
binaries are present **on each host**. Also probes boot survival (restart
policies + enabled-at-boot services) -- the failure shape behind "litellm
didn't come back after a reboot". Writes nothing and runs no state-changing
commands; for remote hosts everything is checked on the *target node*.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..core import cluster, config, render
from ..util import run_command


def _boot_probe(units: list) -> str:
    """Shell one-liner (read-only, ';'-join-safe): containers without a
    restart policy would stay down after a reboot; services not enabled at
    boot never come back."""
    lines = [
        "bad=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.Name}}' "
        "$(docker ps -q 2>/dev/null) 2>/dev/null | grep '^no ' | cut -d' ' -f2-)",
        '[ -n "$bad" ] && printf "%s\n" "$bad" | sed "s#^#     WARN no restart policy (won\'t survive reboot): #"',
        '[ -z "$bad" ] && echo "     ok: every running container has a restart policy"',
    ]
    for u in units:
        lines.append(f"systemctl is-enabled {u} >/dev/null 2>&1 || echo '     WARN: {u} not enabled at boot'")
    return "; ".join(lines)


def _check_host(t, bins: list) -> int:
    """The per-host half: binaries on that node + render its view."""
    cfg, runtime = t.cfg, t.runtime
    missing = []
    for b in bins:
        found = runtime.available(b) if runtime is not None else False
        if not found:
            missing.append(b)
        print(f"   bin {b:<12} {'ok' if found else 'MISSING (will be skipped at runtime)'}")
    try:
        rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-check-")))
    except Exception as e:  # noqa: BLE001 - any render failure == invalid config
        print(f"[RENDER ERROR] {e}", file=sys.stderr)
        return 1
    where = f"{cfg.install_dir_raw} on the node" if t.is_remote else str(cfg.install_dir)
    print(f"   render: OK ({len(rendered)} file(s) would be written to {where})")
    if missing:
        print(f"   WARN: missing binaries: {', '.join(missing)} — they'll be skipped at runtime.")
    units = ["docker"] + (["tailscaled"] if cfg.tailscale().get("enabled", True) else [])
    print("   boot survival:")
    run_command(["sh", "-c", _boot_probe(units)], runtime=runtime, ok=True)
    return 0


def run(args) -> int:
    try:
        cfg = config.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1

    if cfg.active_host_conflicts():
        print(f"[INVALID] two active models share a host: {cfg._conflict_pairs()}", file=sys.stderr)
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
        print("[INVALID] no hosts in config (add a `hosts:` list).", file=sys.stderr)
        return 1

    bins = ["sparkrun", "docker"]
    if cfg.tailscale().get("enabled", True):
        bins.append("tailscale")
    if cfg.cloudflare().get("enabled", False):
        bins.append("cloudflared")

    print(f"== spark-lab check ({cfg.config_path}) ==")
    print(f"  hosts: {', '.join(t.name for t in ts)}")
    rc = cluster.run_on_each(ts, lambda t: _check_host(t, bins))
    if rc == 0:
        print("\nConfig is valid and renderable on every selected host. `spark-lab apply` is safe to run.")
    return rc
