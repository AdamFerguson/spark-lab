"""Command-line interface: argparse + dispatch only (ADR 0001).

All business logic lives in ``sparklab.core`` (the pure engine) and
``sparklab.commands`` (one thin module per subcommand). This module defines the
command tree and routes to it. The runtime seam is injected here (ADR 0002) and
handed to each command via ``args.runtime``.
"""
from __future__ import annotations

import argparse

from .core import runtime as runtime_mod
from .commands import apply, init, status, teardown, upgrade


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml",
                        help="path to config.yaml (default: ./config.yaml)")
    common.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    parser = argparse.ArgumentParser(prog="spark-lab",
                                     description="Self-host an LLM lab on a DGX Spark.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common],
                            help="create config.yaml + .env, generate keys")
    p_init.add_argument("--yes", action="store_true", help="use defaults, no prompts")
    p_init.set_defaults(func=init.run)

    p_apply = sub.add_parser("apply", parents=[common],
                             help="render + converge to config (idempotent)")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="print the plan only; write nothing, run nothing")
    p_apply.add_argument("--apply", dest="apply", action="store_true",
                         help="allow destructive actions (restart the model on recipe change)")
    p_apply.add_argument("--yes", action="store_true", help="alias for --apply")
    p_apply.set_defaults(func=apply.run)

    p_status = sub.add_parser("status", parents=[common],
                              help="show workloads, stack, network status")
    p_status.set_defaults(func=status.run)

    p_teardown = sub.add_parser("teardown", parents=[common],
                                help="stop the model + remove the stack")
    p_teardown.add_argument("--yes", action="store_true", help="actually do it")
    p_teardown.add_argument("--purge", action="store_true",
                            help="also remove named volumes (data loss)")
    p_teardown.set_defaults(func=teardown.run)

    p_upgrade = sub.add_parser("upgrade", parents=[common],
                               help="update sparkrun + images, re-apply")
    p_upgrade.set_defaults(func=upgrade.run)

    args = parser.parse_args(argv)
    args.runtime = runtime_mod.default_runtime()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
