"""`spark-lab logs <service>` — tail logs from the LiteLLM stack.

Port of the old `docker compose logs` flow. Streams through the runtime seam
(ADR 0002); read-only with respect to the node.
"""
from __future__ import annotations

import sys

from ..core import config, node
from ..util import run_command


def run(args) -> int:
    try:
        cfg = config.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1
    runtime = getattr(args, "runtime", None)
    fs, _ = node.node_env(cfg, runtime)
    if not fs.exists("litellm/docker-compose.yml"):
        print(f"(no {cfg.node_path('litellm/docker-compose.yml')} yet — run `spark-lab apply` first)",
              file=sys.stderr)
        return 1
    argv = ["docker", "compose", "-f", fs.path_str("litellm/docker-compose.yml"), "logs",
            "--tail", str(args.lines), args.service]
    if args.follow:
        argv.append("--follow")
    return run_command(argv, runtime=runtime)
