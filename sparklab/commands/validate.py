"""`spark-lab validate` / `check` — pre-flight, read-only.

Confirms the config is usable BEFORE touching the node: schema + secrets resolve,
required binaries are present, and every template renders. Writes nothing and
runs no commands. In remote operator mode the binary pre-flight checks the
*target node*, not this machine.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from ..core import config, render


def run(args) -> int:
    try:
        cfg = config.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1

    runtime = getattr(args, "runtime", None)
    print(f"== spark-lab validate ({cfg.config_path}) ==")
    if cfg.is_remote and getattr(runtime, "is_remote", False):
        print(f"  target: {runtime.label} (remote)")
    print(f"  install dir: {cfg.install_dir_raw if cfg.is_remote else cfg.install_dir}"
          f"{' (on the node)' if cfg.is_remote else ''}")

    # 1. required binaries (report only; absence is a warning, not a failure)
    bins = ["sparkrun", "docker"]
    if cfg.tailscale().get("enabled", True):
        bins.append("tailscale")
    if cfg.cloudflare().get("enabled", False):
        bins.append("cloudflared")
    missing = []
    for b in bins:
        # Remote-aware pre-flight: check the target node through the runtime seam
        # (falls back to a local PATH check when no runtime is injected).
        found = runtime.available(b) if runtime is not None else shutil.which(b) is not None
        if not found:
            missing.append(b)
        print(f"  bin {b:<12} {'ok' if found else 'MISSING (will be skipped at runtime)'}")

    # 2. render check: prove templates + config render without error (throwaway dir)
    try:
        rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-validate-")))
    except Exception as e:  # noqa: BLE001 - any render failure == invalid config
        print(f"[RENDER ERROR] {e}", file=sys.stderr)
        return 1
    where = f"to {cfg.install_dir_raw} on the node" if cfg.is_remote else f"to {cfg.install_dir}"
    print(f"  render: OK ({len(rendered)} file(s) would be written {where})")

    if missing:
        print(f"\nWARN: missing binaries: {', '.join(missing)} — they'll be skipped at runtime.")
    print("\nConfig is valid and renderable. `spark-lab apply` is safe to run.")
    return 0
