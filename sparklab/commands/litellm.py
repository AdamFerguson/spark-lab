"""`spark-lab litellm` — direct gateway control: status | restart.

The manual-restart escape hatch, made correct: `litellm restart` re-renders
the gateway files (config.yaml / model_config.yaml / extra_models.yaml),
writes them if they drifted from the config, `compose restart litellm`s,
polls /health (bounded), prints the live served model list and updates the
node state. `litellm status` reports the same facts without touching
anything (staleness vs config + health + served list).

Logs: `spark-lab logs litellm`. Full-stack changes: use `apply`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..core import cluster, config, converge, inventory, render, state
from ..util import run_command

GW_FILES = ("litellm/config.yaml", "litellm/model_config.yaml",
            "litellm/extra_models.yaml")


def _one(t, do_restart: bool) -> int:
    cfg, runtime = t.cfg, t.runtime
    fs, st = t.env()
    if not fs.base_exists():
        print(f"[ERROR] install dir not found: "
              f"{cfg.install_dir_raw if t.is_remote else cfg.install_dir}",
              file=sys.stderr)
        return 1
    rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-litellm-")))
    gw = {rel: rendered[rel] for rel in GW_FILES if rel in rendered}
    stale = [rel for rel, data in gw.items()
             if st.files.get(rel) != state.sha256_bytes(data)]

    inv = inventory.discover(runtime, cfg)
    print(f"   gateway files: {'in sync with config' if not stale else 'STALE: ' + ', '.join(stale)}")
    g = inv["gateway"]
    if g and g["reachable"]:
        print(f"   healthy: yes (:{g['port']})")
        for name in g["served"]:
            print(f"     serves {name}")
    else:
        print(f"   healthy: NO (:{g['port'] if g else 4000})")

    if not do_restart:
        return 0
    home = runtime.home_path() if runtime is not None else None
    compose_file = cfg.node_path("litellm/docker-compose.yml", home)
    rc = 0
    if stale:
        for rel in stale:
            fs.write(rel, gw[rel])
        print(f"   wrote {len(stale)} stale gateway file(s)")
    rc |= run_command(["docker", "compose", "-f", compose_file, "restart", "litellm"],
                      runtime=runtime)
    rc |= run_command(converge.gateway_health_argv(cfg.litellm.get("port", 4000)),
                      runtime=runtime)
    g = inventory.discover(runtime, cfg)["gateway"]
    if g and g["reachable"]:
        print("   served now: " + ", ".join(g["served"]))
    st.set_state({**st.files, **{rel: state.sha256_bytes(d) for rel, d in gw.items()}},
                 st.model)
    return rc


def run(args) -> int:
    cfg = config.load(args.config)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = [t for t in cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
          if t.cfg.control_plane_enabled()]
    if not ts:
        print("No control-plane hosts selected (nothing runs a gateway).",
              file=sys.stderr)
        return 1
    verb = getattr(args, "litellm_cmd", "status")
    print(f"== spark-lab litellm {verb} ==")
    return cluster.run_on_each(ts, lambda t: _one(t, do_restart=verb == "restart"))
