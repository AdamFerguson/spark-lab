"""`spark-lab teardown` — stop the model + remove the stack."""
from __future__ import annotations

import sys
from pathlib import Path

from ..core import config, converge, state
from ..util import run_command


def run(args) -> int:
    cfg = config.load(args.config)
    if not args.yes:
        print("Refusing to tear down without --yes.", file=sys.stderr)
        print("This stops the model workload and removes the LiteLLM containers", file=sys.stderr)
        print("(named volumes are kept; add --purge to remove them too). --")
        return 1
    sparkrun = converge.find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    # stop by the recipe's file path (sparkrun's bare-name lookup hits its
    # registries, not our install dir) + host targeting (single-node needs --hosts).
    recipe_file = str(Path(cfg.install_dir) / "sparkrun" / "recipes" / f"{cfg.recipe_name}.yaml")
    stop_argv = [sparkrun, "stop", recipe_file]
    if cfg.is_cluster:
        stop_argv += ["--cluster", cfg.cluster_name]
    else:
        stop_argv += ["--hosts", ",".join(str(h) for h in cfg.hosts)]
    print("Stopping model workload...")
    run_command(stop_argv, ok=True, runtime=getattr(args, "runtime", None))
    down_argv = ["docker", "compose", "-f", compose_file, "down"]
    if args.purge:
        down_argv.append("-v")
    print("Tearing down the LiteLLM + monitoring stack...")
    run_command(down_argv, ok=True, runtime=getattr(args, "runtime", None))
    # Clear the recorded state so the next `apply` re-converges from scratch
    # (a teardown leaves the node with no running managed services).
    state.State(cfg.state_dir).clear()
    print("Done. State cleared (re-run `apply` to bring it back up).")
    return 0
