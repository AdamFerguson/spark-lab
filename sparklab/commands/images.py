"""`spark-lab check images` — resolve + report every image the deploy will pull.

Resolves each image through the precedence (env > profile > images map > v1 field
> default) for the active model + enabled stack. Read-only unless ``--probe`` is
given, which additionally runs ``docker manifest inspect`` for each reference via
the runtime seam (ADR 0002) to catch unresolvable tags before they bite.
"""
from __future__ import annotations

import sys

from ..core import config as config_mod
from ..util import run_command

# Order for stable, readable output (model + gateway first, then the stack).
_ORDER = ("model", "litellm", "db", "redis", "prometheus", "grafana",
          "node_exporter", "dcgm_exporter", "cadvisor", "gpu_textfile")


def run(args) -> int:
    try:
        cfg = config_mod.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1

    resolved = cfg.resolved_images()
    runtime = getattr(args, "runtime", None)
    probe = getattr(args, "probe", False)

    print(f"== spark-lab check images (active: {cfg.active_alias}, "
          f"profile: {cfg.profile}) ==")
    ok = True
    for key in _ORDER:
        if key not in resolved:
            continue  # not enabled for this config (e.g. redis off)
        ref = resolved.get(key)
        if not ref:
            print(f"  {key:<14} MISSING  (no image set for {key})")
            if key == "model":
                ok = False
            continue
        status = "ok"
        if probe:
            rc = run_command(["docker", "manifest", "inspect", ref], runtime=runtime)
            if rc != 0:
                status = "UNRESOLVABLE (docker manifest inspect failed)"
                ok = False
        print(f"  {key:<14} {ref}  [{status}]")

    if not ok:
        print("\nOne or more images are unresolvable. Fix the config (or the probe "
              "failure) before applying.")
        return 1
    print("\nAll images resolve.")
    return 0
