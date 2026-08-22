"""`spark-lab init` -- system precheck, then create config.yaml + .env."""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

from . import system


def run(args) -> int:
    runtime = getattr(args, "runtime", None)
    _system_precheck(args, runtime)

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
    print("  3. Run `spark-lab apply --dry-run` to preview the plan.")
    print("  4. Run `spark-lab apply` (add --apply to restart the model on recipe change).")
    return 0


def _system_precheck(args, runtime) -> None:
    """Detect required/optional tools; report, and (interactively) offer to install
    the missing required ones. Never blocks config creation."""
    if runtime is None:
        return
    results = system.detect(runtime)
    system.print_table(results)
    req_missing = system.missing(results, required_only=True)
    if not req_missing:
        print("All required tools present.")
        return
    print("Missing required tool(s): " + ", ".join(r["name"] for r in req_missing))
    if getattr(args, "yes", False):
        print("To install them, run: `spark-lab check system --install`")
        return
    try:
        ans = input("Install the missing required tools now? [y/N] ")
    except (EOFError, OSError):
        ans = "n"
    if ans.strip().lower() in ("y", "yes"):
        system.install(results, runtime)
    else:
        print("Skipped. You can install later with `spark-lab check system --install`.")


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
