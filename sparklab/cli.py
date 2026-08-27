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
from .commands import (adopt, apply, check as check_cmd, images, init, logs, migrate, model, recipes,
                       status, system, teardown, upgrade, validate)


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml",
                        help="path to config.yaml (default: ./config.yaml)")
    common.add_argument("--hosts", default=None,
                        help="comma-separated host names from the config's hosts: list "
                             "(default: all hosts)")
    common.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    parser = argparse.ArgumentParser(prog="spark-lab",
                                     description="Self-host an LLM lab on a DGX Spark.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common],
                            help="create config.yaml + .env, generate keys; with --hosts: bootstrap those hosts")
    p_init.add_argument("--yes", action="store_true",
                        help="use defaults, no prompts (and actually install/prepare in host bootstrap)")
    p_init.add_argument("--all", action="store_true",
                        help="host bootstrap: also install missing optional tools")
    p_init.set_defaults(func=init.run)

    p_apply = sub.add_parser("apply", parents=[common],
                             help="render + converge to config (idempotent)")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="print the plan only; write nothing, run nothing")
    p_apply.add_argument("--restart-model", dest="restart_model", action="store_true",
                         help="allow destructive actions (stop/restart the model on recipe change)")
    p_apply.add_argument("--apply", dest="apply", action="store_true", help=argparse.SUPPRESS)
    p_apply.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    p_apply.add_argument("--diff", action="store_true",
                         help="with --dry-run, show a diff of each changed file")
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
    p_upgrade.add_argument("--yes", action="store_true", help="actually upgrade (restarts models)")
    p_upgrade.set_defaults(func=upgrade.run)

    p_validate = sub.add_parser("validate", parents=[common],
                                help=argparse.SUPPRESS)   # alias: `check`
    p_validate.set_defaults(func=validate.run)

    p_check = sub.add_parser("check", parents=[common],
                             help="pre-execution checks (config; + --images / --system)")
    p_check.add_argument("what", nargs="?", choices=["config", "images", "system"],
                         help="legacy form; `check` alone = config pre-flight")
    p_check.add_argument("--images", action="store_true",
                         help="also resolve the stack images")
    p_check.add_argument("--probe", action="store_true",
                         help="with images: probe each (docker manifest inspect)")
    p_check.add_argument("--system", action="store_true",
                         help="also run the per-host system precheck")
    p_check.add_argument("--install", action="store_true",
                         help="with --system: install the missing required tools")
    p_check.add_argument("--all", action="store_true",
                         help="with --install: also install missing optional tools")
    p_check.set_defaults(func=check_cmd.run)

    p_doctor = sub.add_parser("doctor", parents=[common],
                              help=argparse.SUPPRESS)   # alias: `check --system`
    p_doctor.add_argument("--install", action="store_true", help=argparse.SUPPRESS)
    p_doctor.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    p_doctor.set_defaults(func=system.check)

    p_migrate = sub.add_parser("migrate", parents=[common],
                               help="rewrite a v1/v2 config.yaml to schema v3 (idempotent)")
    p_migrate.add_argument("--dry-run", action="store_true",
                           help="print the v2 form without writing")
    p_migrate.set_defaults(func=migrate.run)

    p_adopt = sub.add_parser("adopt", parents=[common],
                             help="take over an existing running install (read-only; writes only state)")
    p_adopt.add_argument("--dry-run", action="store_true", help="report; write no state")
    p_adopt.set_defaults(func=adopt.run)

    p_model = sub.add_parser("model", help="model workload actions (the stack keeps running)")
    model_sub = p_model.add_subparsers(dest="model_cmd", required=True)
    p_model_up = model_sub.add_parser("up", parents=[common],
                                      help="scale a model up: add host(s) to its hosts: and converge")
    p_model_up.add_argument("model", help="model name from the config's models: map")
    p_model_up.set_defaults(func=model.up)
    p_model_down = model_sub.add_parser("down", parents=[common],
                                        help="scale a model down: remove host(s) from its hosts: and stop")
    p_model_down.add_argument("model", help="model name from the config's models: map")
    p_model_down.add_argument("--yes", action="store_true", help="actually stop + update config")
    p_model_down.set_defaults(func=model.down)
    p_model_stop = model_sub.add_parser("stop", parents=[common],
                                        help="stop the model workload now (config unchanged; next apply restarts)")
    p_model_stop.add_argument("--yes", action="store_true", help="actually stop the model")
    p_model_stop.set_defaults(func=model.stop)

    p_recipes = sub.add_parser("recipes", help="discover + convert model recipes (ADR 0003)")
    recipes_sub = p_recipes.add_subparsers(dest="recipes_cmd", required=True)
    p_r_search = recipes_sub.add_parser("search", parents=[common],
                                        help="fan a query out to enabled sources")
    p_r_search.add_argument("query", help="free-text / tag query")
    p_r_search.add_argument("--source", help="limit to one source alias")
    p_r_search.set_defaults(func=recipes.search)
    p_r_list = recipes_sub.add_parser("list", parents=[common],
                                      help="enumerate one source (or all)")
    p_r_list.add_argument("source", nargs="?", help="source alias (default: all)")
    p_r_list.set_defaults(func=recipes.list_)
    p_r_show = recipes_sub.add_parser("show", parents=[common],
                                      help="resolve <source://ref>, print metadata + body")
    p_r_show.add_argument("reference", help="<source://reference> (or just <reference>)")
    p_r_show.set_defaults(func=recipes.show)
    p_r_convert = recipes_sub.add_parser("convert", parents=[common],
                                         help="produce a sparkrun candidate recipe (never applied)")
    p_r_convert.add_argument("reference", help="<source://reference>")
    p_r_convert.add_argument("--out", help="output path (default recipes/candidates/<ref>.yaml)")
    p_r_convert.add_argument("--dry-run", action="store_true",
                             help="print the candidate without writing")
    p_r_convert.set_defaults(func=recipes.convert)

    p_logs = sub.add_parser("logs", parents=[common],
                            help="tail logs from a stack service (one host; --hosts to pick)")
    p_logs.add_argument("service",
                        help="compose service (litellm, db, redis, prometheus, grafana)")
    p_logs.add_argument("--lines", type=int, default=100, help="lines to tail (default 100)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="follow the log")
    p_logs.set_defaults(func=logs.run)

    _hide_suppressed_subcommands(parser)

    args = parser.parse_args(argv)
    args.runtime = build_runtime(args.config)
    return args.func(args)


def _hide_suppressed_subcommands(parser: argparse.ArgumentParser) -> None:
    """argparse leaks the ==SUPPRESS== sentinel into the subcommand list; strip it."""
    for action in parser._actions:   # noqa: SLF001 - argparse has no public API for this
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:   # noqa: SLF001
                if choice.help == argparse.SUPPRESS:
                    choice.help = None


def build_runtime(config_path: str):
    """The runtime for this run: local, or remote when the config sets
    ``install.remote.host``.

    Commands that don't target a node (``init``, ``recipes``, ``migrate``) may
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
