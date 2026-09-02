"""`spark-lab logs <service>` — tail logs from the LiteLLM stack on one host.

Streams through the runtime seam (ADR 0002); read-only with respect to the node.
Log streaming is one host at a time: with several hosts selected, name one with
``--hosts``.
"""

from __future__ import annotations

import sys

from ..core import cluster, config
from ..util import run_command


def _logs_one(t, service: str, lines: int, follow: bool) -> int:
    cfg, runtime = t.cfg, t.runtime
    fs, _ = t.env()
    if not fs.exists("litellm/docker-compose.yml"):
        print(
            f"(no {cfg.node_path('litellm/docker-compose.yml', runtime.home_path() if runtime else None)} "
            f"yet — run `spark-lab apply` first)",
            file=sys.stderr,
        )
        return 1
    argv = ["docker", "compose", "-f", fs.path_str("litellm/docker-compose.yml"), "logs", "--tail", str(lines), service]
    if follow:
        argv.append("--follow")
    return run_command(argv, runtime=runtime)


def run(args) -> int:
    try:
        cfg = config.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    if len(ts) > 1:
        print(
            "Several hosts selected; `logs` streams one host at a time. "
            f"Use --hosts {ts[0].name} (options: {', '.join(t.name for t in ts)}).",
            file=sys.stderr,
        )
        return 1
    t = ts[0]
    print(f"==> [{t.name}] {t.label}")
    return _logs_one(t, args.service, args.lines, args.follow)
