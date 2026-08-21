"""Command-line interface: init / apply / status / teardown / upgrade."""

from __future__ import annotations

import argparse
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as config_mod
from . import converge, render, state as state_mod
from .config import load


def _generate_env(config_path: Path, yes: bool) -> None:
    """Create .env from .env.example and fill in generated secrets."""
    repo = config_path.parent
    env_path = repo / ".env"
    if env_path.is_file():
        print(f".env already exists at {env_path} (left untouched).")
        return
    example = repo / ".env.example"
    base = example.read_text() if example.is_file() else ""
    values = {
        "LITELLM_MASTER_KEY": "sk-" + secrets.token_hex(32),
        "LITELLM_SALT_KEY": "sk-" + secrets.token_hex(32),
        "LITELLM_DB_PASSWORD": secrets.token_hex(16),
        "GRAFANA_ADMIN_PASSWORD": secrets.token_hex(16),
        "HF_TOKEN": "",
        "CF_TUNNEL_TOKEN": "",
    }
    if not yes:
        print("Generated keys for your .env (press Ctrl+C to abort and set your own).")
    lines = []
    for raw in base.splitlines():
        key = raw.split("=", 1)[0].strip()
        if "=" in raw and key in values:
            lines.append(f"{key}={values[key]}")
        else:
            lines.append(raw)
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    print(f"Wrote {env_path} (chmod 600). Fill in HF_TOKEN / CF_TUNNEL_TOKEN as needed.")


def cmd_init(args) -> int:
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    repo = cfg_path.parent
    if not cfg_path.is_file():
        example = repo / "config.example.yaml"
        if example.is_file():
            cfg_path.write_text(example.read_text())
            print(f"Created {cfg_path} from config.example.yaml -- edit it for your setup.")
        else:
            print(f"No config or config.example.yaml found in {repo}.", file=sys.stderr)
            return 1
    _generate_env(cfg_path, args.yes)
    print("\nNext steps:")
    print(f"  1. Edit {cfg_path} (model, ports, dashboards, network).")
    print(f"  2. Review the secrets in {repo / '.env'}.")
    print(f"  3. Run `spark-lab apply --dry-run` to preview the plan.")
    print(f"  4. Run `spark-lab apply` (add --apply to restart the model on recipe change).")
    return 0


def cmd_apply(args) -> int:
    cfg = load(args.config)
    dry = getattr(args, "dry_run", False)
    allow_restart = bool(getattr(args, "apply", False) or getattr(args, "yes", False))

    print(f"== spark-lab apply {'[dry-run]' if dry else ''} ==")
    print(f"   config     : {cfg.config_path}")
    print(f"   install dir: {cfg.install_dir}")
    print(f"   hosts      : {', '.join(str(h) for h in cfg.hosts)}"
          f"{'  (cluster)' if cfg.is_cluster else ''}")

    # Dry-run renders into a throwaway dir so it truly writes nothing to the repo.
    import tempfile
    out_dir = Path(tempfile.mkdtemp(prefix="sparklab-dry-")) if dry else cfg.deploy_dir
    rendered = render.render(cfg, out_dir)
    state = state_mod.State(cfg.state_dir)
    plan = converge.build_plan(cfg, rendered, state.files, state.model, allow_restart)

    print("\nFile changes vs last apply:")
    if plan.file_changes:
        for rel, kind in plan.file_changes:
            print(f"   {kind:<7} {rel}")
    else:
        print("   (none -- already converged)")

    for note in plan.notes:
        print(f"   note: {note}")

    print("\nActions:")
    for desc, _ in plan.commands:
        print(f"   - {desc}")

    if dry:
        print("\n[dry-run] No files written, no commands executed.")
        return 0

    written = converge.write_files(cfg, rendered, dry_run=False)
    if written:
        print(f"\nWrote {len(written)} file(s) to {cfg.install_dir}")
    rc = converge.execute(plan, dry_run=False)
    if rc == 0:
        new_files = converge.compute_files_after_apply(rendered)
        has_model = plan.current_hash is not None
        converged_after = (not plan.model_restart_pending) if has_model else allow_restart
        new_model = converge.compute_model_after_apply(
            state.model, cfg.recipe_name, plan.current_hash, converged_after)
        state.set_state(new_files, new_model)
        print("\nConverged. State updated.")
        if plan.model_restart_pending:
            print("NOTE: a model change is still pending (not restarted). "
                  "Re-run with `spark-lab apply --apply` to restart the model.")
    return rc


def cmd_status(args) -> int:
    cfg = load(args.config)
    sparkrun = converge.find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    print("== sparkrun ==")
    _run([sparkrun, "status"], ok=True)
    print("\n== docker compose ==")
    _run(["docker", "compose", "-f", compose_file, "ps"], ok=True)
    print("\n== tailscale ==")
    _run(["tailscale", "status"], ok=True)
    if cfg.cloudflare().get("enabled", False):
        print("\n== cloudflare ==")
        _run(["systemctl", "is-active", "cloudflared"], ok=True)
    return 0


def cmd_teardown(args) -> int:
    cfg = load(args.config)
    if not args.yes:
        print("Refusing to tear down without --yes.", file=sys.stderr)
        print("This stops the model workload and removes the LiteLLM containers", file=sys.stderr)
        print(f"(named volumes are kept; add --purge to remove them too). --")
        return 1
    sparkrun = converge.find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    stop_argv = [sparkrun, "stop", cfg.recipe_name]
    if cfg.is_cluster:
        stop_argv += ["--cluster", cfg.cluster_name]
    print("Stopping model workload...")
    _run(stop_argv, ok=True)
    down_argv = ["docker", "compose", "-f", compose_file, "down"]
    if args.purge:
        down_argv.append("-v")
    print("Tearing down the LiteLLM + monitoring stack...")
    _run(down_argv, ok=True)
    print("Done. (Volumes kept unless --purge was passed.)")
    return 0


def cmd_upgrade(args) -> int:
    cfg = load(args.config)
    repo_root = Path(__file__).resolve().parent.parent
    req = repo_root / "requirements.txt"
    if req.is_file():
        print("Updating spark-lab engine dependencies...")
        _run([sys.executable, "-m", "pip", "install", "-U", "-r", str(req)], ok=True)
    sparkrun = converge.find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    print("Updating sparkrun + recipe registries...")
    _run([sparkrun, "update"], ok=True)
    print("Pulling latest stack images...")
    _run(["docker", "compose", "-f", compose_file, "pull"], ok=True)
    print("Re-applying (model restart allowed)...")
    args.apply = True
    return cmd_apply(args)


def _run(argv, ok: bool = False) -> int:
    """Run a read-only/operational command, streaming output."""
    if not shutil.which(argv[0]):
        print(f"(skipping: '{argv[0]}' not found on PATH)")
        return 0
    result = subprocess.run(argv)
    return result.returncode if ok else result.returncode


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
    p_init.set_defaults(func=cmd_init)

    p_apply = sub.add_parser("apply", parents=[common],
                             help="render + converge to config (idempotent)")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="print the plan only; write nothing, run nothing")
    p_apply.add_argument("--apply", dest="apply", action="store_true",
                         help="allow destructive actions (restart the model on recipe change)")
    p_apply.add_argument("--yes", action="store_true", help="alias for --apply")
    p_apply.set_defaults(func=cmd_apply)

    p_status = sub.add_parser("status", parents=[common],
                              help="show workloads, stack, network status")
    p_status.set_defaults(func=cmd_status)

    p_teardown = sub.add_parser("teardown", parents=[common],
                                help="stop the model + remove the stack")
    p_teardown.add_argument("--yes", action="store_true", help="actually do it")
    p_teardown.add_argument("--purge", action="store_true",
                            help="also remove named volumes (data loss)")
    p_teardown.set_defaults(func=cmd_teardown)

    p_upgrade = sub.add_parser("upgrade", parents=[common],
                               help="update sparkrun + images, re-apply")
    p_upgrade.set_defaults(func=cmd_upgrade)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
