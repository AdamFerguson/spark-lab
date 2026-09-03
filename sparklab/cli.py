"""Command-line interface: argparse + dispatch only (ADR 0001).

All business logic lives in ``sparklab.core`` (the pure engine) and
``sparklab.commands`` (one thin module per subcommand). This module defines the
command tree and routes to it. The runtime seam is injected here (ADR 0002) and
handed to each command via ``args.runtime``.
"""

from __future__ import annotations

import argparse

from .core import config as config_mod
from .core import runtime as runtime_mod
from .commands import (
    adopt,
    apply,
    check as check_cmd,
    expose,
    init,
    litellm,
    logs,
    model,
    status,
    swap,
    sync,
    teardown,
    zoo,
)


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    common.add_argument(
        "--hosts", default=None, help="comma-separated host names from the config's hosts: list (default: all hosts)"
    )
    common.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    parser = argparse.ArgumentParser(prog="spark-lab", description="Self-host an LLM lab on a DGX Spark.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common], help="create config.yaml + .env with generated keys")
    p_init.add_argument("--yes", action="store_true", help="non-interactive (skip the review pause)")
    p_init.set_defaults(func=init.run)

    p_apply = sub.add_parser("apply", parents=[common], help="render + converge to config (idempotent)")
    p_apply.add_argument("--dry-run", action="store_true", help="print the plan only; write nothing, run nothing")
    p_apply.add_argument(
        "--restart-model",
        dest="restart_model",
        action="store_true",
        help="allow destructive actions (stop/restart the model on recipe change)",
    )
    p_apply.add_argument(
        "--no-model",
        dest="no_model",
        action="store_true",
        help="reconcile control plane + gateway only; never launch/stop a "
        "sparkrun model (use when the model is run outside spark-lab)",
    )
    p_apply.add_argument("--diff", action="store_true", help="with --dry-run, show a diff of each changed file")
    p_apply.set_defaults(func=apply.run)

    p_status = sub.add_parser("status", parents=[common], help="show workloads, stack, network status")
    p_status.set_defaults(func=status.run)

    p_teardown = sub.add_parser("teardown", parents=[common], help="stop the model + remove the stack")
    p_teardown.add_argument("--yes", action="store_true", help="actually do it")
    p_teardown.add_argument("--purge", action="store_true", help="also remove named volumes (data loss)")
    p_teardown.set_defaults(func=teardown.run)

    p_check = sub.add_parser(
        "check", parents=[common], help="read-only pre-flight: config + render + binaries per host"
    )
    p_check.set_defaults(func=check_cmd.run)

    p_adopt = sub.add_parser(
        "adopt", parents=[common], help="take over an existing running install (read-only; writes only state)"
    )
    p_adopt.add_argument("--dry-run", action="store_true", help="report; write no state")
    p_adopt.set_defaults(func=adopt.run)

    p_expose = sub.add_parser(
        "expose",
        parents=[common],
        help="put a running engine behind the gateway (extra_models entry + gateway-only apply)",
    )
    p_expose.add_argument("host", help="host NAME from the config's hosts: (or NAME:PORT; default port 8000)")
    p_expose.add_argument("--port", type=int, help="engine port (default 8000)")
    p_expose.add_argument("--served-model", help="which id the engine serves (default: its first)")
    p_expose.add_argument("--public-name", help="gateway name to expose it under (default: the served id)")
    p_expose.add_argument("--dry-run", action="store_true", help="probe + show the entry; write nothing")
    p_expose.set_defaults(func=expose.run)

    p_sync = sub.add_parser(
        "sync",
        parents=[common],
        help="pull live reality into view: unexposed engines, ghosts, drift (--write fixes what it can)",
    )
    p_sync.add_argument(
        "--write",
        action="store_true",
        help="add extra_models entries for unexposed engines + refresh node state (model workloads are never touched)",
    )
    p_sync.set_defaults(func=sync.run)

    p_lit = sub.add_parser(
        "litellm", help="gateway control (stack-wide changes: use apply; logs: spark-lab logs litellm)"
    )
    lit_sub = p_lit.add_subparsers(dest="litellm_cmd", required=True)
    p_lit_status = lit_sub.add_parser("status", parents=[common], help="gateway staleness + health + served list")
    p_lit_status.set_defaults(func=litellm.run)
    p_lit_restart = lit_sub.add_parser(
        "restart", parents=[common], help="write stale gateway files, restart, verify health, show served list"
    )
    p_lit_restart.set_defaults(func=litellm.run)

    p_zoo = sub.add_parser("zoo", help="model zoo (llama-swap, ADR-0010)")
    zoo_sub = p_zoo.add_subparsers(dest="zoo_cmd", required=True)
    p_zoo_prepare = zoo_sub.add_parser(
        "prepare",
        parents=[common],
        help="converge zoo files + install/start the llama-swap user service (idempotent)",
    )
    p_zoo_prepare.set_defaults(func=zoo.run)

    p_swap = sub.add_parser(
        "swap",
        help="inspect / steer the model zoo (daily use: none -- requesting a zoo model loads it automatically)",
    )
    swap_sub = p_swap.add_subparsers(dest="swap_cmd", required=True)
    p_swap_status = swap_sub.add_parser("status", parents=[common], help="which zoo models are resident now")
    p_swap_status.set_defaults(func=swap.run)
    p_swap_unload = swap_sub.add_parser("unload", parents=[common], help="force-unload a model (or all with --yes)")
    p_swap_unload.add_argument("model", nargs="?", help="zoo model alias (default: all)")
    p_swap_unload.add_argument("--yes", action="store_true", help="required to unload ALL")
    p_swap_unload.set_defaults(func=swap.run)

    p_model = sub.add_parser("model", help="model workload actions (the stack keeps running)")
    model_sub = p_model.add_subparsers(dest="model_cmd", required=True)
    p_model_up = model_sub.add_parser(
        "up", parents=[common], help="scale a model up: add host(s) to its hosts: and converge"
    )
    p_model_up.add_argument("model", help="model name from the config's models: map")
    p_model_up.set_defaults(func=model.up)
    p_model_down = model_sub.add_parser(
        "down", parents=[common], help="scale a model down: remove host(s) from its hosts: and stop"
    )
    p_model_down.add_argument("model", help="model name from the config's models: map")
    p_model_down.add_argument("--yes", action="store_true", help="actually stop + update config")
    p_model_down.set_defaults(func=model.down)
    p_model_stop = model_sub.add_parser(
        "stop", parents=[common], help="stop the model workload now (config unchanged; next apply restarts)"
    )
    p_model_stop.add_argument("--yes", action="store_true", help="actually stop the model")
    p_model_stop.set_defaults(func=model.stop)

    p_logs = sub.add_parser("logs", parents=[common], help="tail logs from a stack service (one host; --hosts to pick)")
    p_logs.add_argument("service", help="compose service (litellm, db, redis, prometheus, grafana)")
    p_logs.add_argument("--lines", type=int, default=100, help="lines to tail (default 100)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="follow the log")
    p_logs.set_defaults(func=logs.run)

    _hide_suppressed_subcommands(parser)

    args = parser.parse_args(argv)
    args.runtime = build_runtime(args.config)
    return args.func(args)


def _hide_suppressed_subcommands(parser: argparse.ArgumentParser) -> None:
    """argparse leaks the ==SUPPRESS== sentinel into the subcommand list; strip it."""
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API for this
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:  # noqa: SLF001
                if choice.help == argparse.SUPPRESS:
                    choice.help = None


def build_runtime(config_path: str):
    """The runtime for this run: local, or remote when the config sets
    ``install.remote.host``.

    Commands that don't target a node (``init``) may
    run before/without a config; any load failure falls back to the local
    runtime and the command itself reports the problem.
    """
    try:
        cfg = config_mod.load(config_path)
    except Exception:
        return runtime_mod.default_runtime()
    return runtime_mod.runtime_for(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
