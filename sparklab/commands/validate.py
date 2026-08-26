"""`spark-lab validate` / `check` — pre-flight, read-only.

Confirms the config is usable BEFORE touching the node: schema + secrets resolve,
required binaries are present, and every template renders. Writes nothing and
runs no commands.
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

    print(f"== spark-lab validate ({cfg.config_path}) ==")
    print(f"  install dir: {cfg.install_dir}")

    # 1. required binaries (report only; absence is a warning, not a failure)
    bins = ["sparkrun", "docker"]
    if cfg.tailscale().get("enabled", True):
        bins.append("tailscale")
    if cfg.cloudflare().get("enabled", False):
        bins.append("cloudflared")
    missing = []
    for b in bins:
        found = shutil.which(b)
        if not found:
            missing.append(b)
        print(f"  bin {b:<12} {'ok' if found else 'MISSING (will be skipped at runtime)'}")

    # 2. render check: prove templates + config render without error (throwaway dir)
    try:
        rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-validate-")))
    except Exception as e:  # noqa: BLE001 - any render failure == invalid config
        print(f"[RENDER ERROR] {e}", file=sys.stderr)
        return 1
    print(f"  render: OK ({len(rendered)} file(s) would be written to {cfg.install_dir})")

    if missing:
        print(f"\nWARN: missing binaries: {', '.join(missing)} — they'll be skipped at runtime.")
    print("\nConfig is valid and renderable. `spark-lab apply` is safe to run.")
    return 0
