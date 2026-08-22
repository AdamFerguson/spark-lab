"""`spark-lab teardown` — stop the model + remove the stack."""
from __future__ import annotations

import sys
from pathlib import Path

from ..core import config, converge
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
    stop_argv = [sparkrun, "stop", cfg.recipe_name]
    if cfg.is_cluster:
        stop_argv += ["--cluster", cfg.cluster_name]
    print("Stopping model workload...")
    run_command(stop_argv, ok=True, runtime=getattr(args, "runtime", None))
    down_argv = ["docker", "compose", "-f", compose_file, "down"]
    if args.purge:
        down_argv.append("-v")
    print("Tearing down the LiteLLM + monitoring stack...")
    run_command(down_argv, ok=True, runtime=getattr(args, "runtime", None))
    print("Done. (Volumes kept unless --purge was passed.)")
    return 0
