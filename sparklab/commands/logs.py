"""`spark-lab logs <service>` — tail logs from the LiteLLM stack.

Port of the old `docker compose logs` flow. Streams through the runtime seam
(ADR 0002); read-only with respect to the node.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..core import config
from ..util import run_command


def run(args) -> int:
    try:
        cfg = config.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1
    compose_file = Path(cfg.install_dir) / "litellm" / "docker-compose.yml"
    if not compose_file.is_file():
        print(f"(no {compose_file} yet — run `spark-lab apply` first)", file=sys.stderr)
        return 1
    argv = ["docker", "compose", "-f", str(compose_file), "logs",
            "--tail", str(args.lines), args.service]
    if args.follow:
        argv.append("--follow")
    return run_command(argv, runtime=getattr(args, "runtime", None))
