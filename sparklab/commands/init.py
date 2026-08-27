"""`spark-lab init` -- create the local config, or bootstrap managed hosts.

Two modes:

* ``spark-lab init`` (no ``--hosts``) -- create ``config.yaml`` + ``.env`` in the
  current checkout and generate fresh keys (historical behavior).
* ``spark-lab init --hosts a,b [--yes] [--all]`` -- idempotently prepare the
  selected host(s) for management: report/install the required tools
  (``check system``), ensure the spark-lab git checkout (clone from
  ``install.repo_url`` when missing; fast-forward when the working tree is
  clean), ensure the install dir exists, and bring tailscale up. Safe to re-run
  any time -- newly added dependencies just get installed.

Without ``--yes`` the host bootstrap is report-only (it shows what would be
installed/prepared, same as a plain ``check system``).
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

from ..core import cluster, config
from ..util import run_command
from . import system


def run(args) -> int:
    if cluster.parse_hosts_arg(getattr(args, "hosts", None)):
        return _bootstrap(args)
    return _create(args)


def _bootstrap(args) -> int:
    """Idempotent host preparation for one or more selected hosts."""
    cfg = config.load(args.config)
    names = cluster.parse_hosts_arg(args.hosts)
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    yes = bool(getattr(args, "yes", False))
    include_optional = bool(getattr(args, "all", False))

    print(f"== spark-lab init (host bootstrap) ==")
    suffix = "" if yes else "   [report only: add --yes to install/prepare]"
    print(f"   hosts: {', '.join(t.name for t in ts)}{suffix}")
    return cluster.run_on_each(ts, lambda t: _bootstrap_one(t, yes, include_optional))


def _shell(runtime, expr: str) -> int:
    """Run a shell expression through the runtime seam (builtins like `test`/
    `mkdir` need a real shell, and `run_command` PATH-gates its first arg)."""
    return runtime.run(["sh", "-lc", expr]).returncode


def _bootstrap_one(t, do_it: bool, include_optional: bool) -> int:
    """Prepare one host: tools -> git checkout -> install dir -> tailscale."""
    cfg, runtime = t.cfg, t.runtime
    rc = 0

    # 1) tools (check system; install when --yes)
    tool_rc = system._check_one(runtime, do_install=do_it, include_optional=include_optional)
    if tool_rc != 0:
        rc = 1   # report the rest; don't abort the host preparation

    home = runtime.home_path() if runtime is not None else None
    install_dir = cfg.node_path("", home).rstrip("/")
    repo_dir = cfg.repo_dir
    if home:
        repo_dir = home + repo_dir[1:] if repo_dir.startswith("~/") else home
    elif repo_dir.startswith("~"):
        import os as _os
        repo_dir = _os.path.expanduser(repo_dir)

    # 2) spark-lab checkout (state + upgrade live here on the node)
    if not do_it:
        print(f"   (would ensure) spark-lab checkout: {repo_dir}")
    elif _shell(runtime, "test -d " + _q(repo_dir + "/.git")) != 0:
        repo_url = str(cfg.install.get("repo_url") or "")
        if not repo_url:
            print(f"   [!] no spark-lab checkout at {repo_dir} and no install.repo_url in the "
                  f"config; clone it manually (state + `upgrade` need it).")
        else:
            print(f"   cloning spark-lab -> {repo_dir}")
            rc = run_command(["git", "clone", repo_url, repo_dir], runtime=runtime) or rc
    else:
        r = runtime.run(["sh", "-lc", "git -C " + _q(repo_dir) + " status --porcelain"])
        if getattr(r, "stdout", "") and str(r.stdout).strip():
            print(f"   spark-lab checkout has local changes -- skipping the refresh "
                  f"(review {repo_dir} manually).")
        else:
            print(f"   refreshing spark-lab checkout -> main")
            run_command(["git", "-C", repo_dir, "fetch", "--quiet", "origin"],
                        ok=True, runtime=runtime)
            rc = run_command(["git", "-C", repo_dir, "pull", "--ff-only", "--quiet",
                              "origin", "main"], runtime=runtime) or rc

    # 3) install dir
    if do_it:
        _shell(runtime, "mkdir -p " + _q(install_dir))
        print(f"   install dir ready: {install_dir}")
    else:
        print(f"   (would ensure) install dir: {install_dir}")

    # 4) tailscale up (best-effort: needs root on most nodes)
    if cfg.tailscale().get("enabled", True):
        if do_it:
            if run_command(["systemctl", "enable", "--now", "tailscaled"], ok=True,
                           runtime=runtime) != 0:
                print("   (tailscaled could not be enabled here -- usually needs root)")
        else:
            print("   (would ensure) tailscaled enabled + running")
    return rc


def _q(p: str) -> str:
    import shlex
    return shlex.quote(p)


def _create(args) -> int:
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
    print(f"  1. Edit {cfg_path} (hosts, model, ports, dashboards, network).")
    print(f"  2. Review the secrets in {repo / '.env'}.")
    print("  3. Run `spark-lab apply --dry-run` to preview the plan.")
    print("  4. Run `spark-lab apply` (add --restart-model to restart the model on recipe change).")
    print("  5. `spark-lab init --hosts <hosts> --yes` bootstraps the managed nodes.")
    return 0


def _system_precheck(args, runtime) -> None:
    """Detect required/optional tools; report, and (interactively) offer to install
    the missing required ones. Never blocks config creation."""
    if runtime is None:
        return
    results = system.detect(runtime)
    system.print_table(results)
    caps = system.check_capabilities(runtime)
    system.print_capabilities(caps)
    cap_fix = system.caps_needing_fix(caps)
    for c in cap_fix:
        print(f"\n! {c['name']} -- {c['why']}")
        print(f"  to fix (needs sudo, then a fresh shell): {c['fix']}")
    req_missing = system.missing(results, required_only=True)
    if not req_missing:
        if not cap_fix:
            print("\nAll required tools present.")
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
