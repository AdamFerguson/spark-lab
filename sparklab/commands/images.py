"""`spark-lab check images` — resolve + report every image the deploy will pull.

Resolves each image through the precedence (env > profile > images map > v1 field
> default) for the active model + enabled stack. Read-only unless ``--probe`` is
given, which additionally runs ``docker manifest inspect`` for each reference via
the runtime seam (ADR 0002) to catch unresolvable tags before they bite.
"""
from __future__ import annotations

import sys

from ..core import cluster, config as config_mod
from ..util import run_command

# Order for stable, readable output (model + gateway first, then the stack).
_ORDER = ("model", "litellm", "db", "redis", "prometheus", "grafana",
          "node_exporter", "dcgm_exporter", "cadvisor", "gpu_textfile")


def _images_one(t, probe: bool) -> int:
    cfg = t.cfg
    resolved = cfg.resolved_images()
    runtime = t.runtime

    print(f"== spark-lab check images (active: {cfg.active_alias or '(none)'}, "
          f"profile: {cfg.profile}) ==")
    ok = True
    for key in _ORDER:
        if key not in resolved:
            continue  # not enabled for this config (e.g. redis off)
        if key == "model" and not cfg.model:
            continue  # this host serves no model (control-plane only)
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


def run(args) -> int:
    try:
        cfg = config_mod.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    probe = getattr(args, "probe", False)
    return cluster.run_on_each(ts, lambda t: _images_one(t, probe))
