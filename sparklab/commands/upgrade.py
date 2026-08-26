"""`spark-lab upgrade` — update sparkrun + images, re-apply."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..core import config, converge
from ..util import run_command
from . import apply as apply_cmd


def _refresh_engine_deps(repo_dir, runtime):
    """Refresh the engine deps (on this machine, or on the node in remote mode).

    ``repo_dir`` is the spark-lab checkout to refresh (a local Path or a
    node-side path string). uv-first (re-resolve the lock + re-sync the
    runtime env); fall back to `pip install -U -e .` when uv is absent. uv
    detection routes through the runtime seam so it is testable and works
    against the remote node.
    """
    have_uv = runtime.available("uv") if runtime is not None else shutil.which("uv") is not None
    if have_uv:
        run_command(["uv", "lock", "--upgrade", "--directory", str(repo_dir)], ok=True, runtime=runtime)
        run_command(["uv", "sync", "--no-default-groups", "--directory", str(repo_dir)], ok=True, runtime=runtime)
    else:
        # Remote: use the node's python3 (the operator's sys.executable lives on
        # the wrong machine). Local: keep the exact interpreter that runs spark-lab.
        remote = runtime is not None and getattr(runtime, "is_remote", False)
        python = "python3" if remote else sys.executable
        run_command([python, "-m", "pip", "install", "-U", "-e", str(repo_dir)],
                    ok=True, runtime=runtime)


def run(args) -> int:
    cfg = config.load(args.config)
    runtime = getattr(args, "runtime", None)

    if cfg.is_remote and getattr(runtime, "is_remote", False):
        # Remote: refresh the *node's* checkout (that's where its engine +
        # venv live); everything below then runs over the same SSH session.
        repo_dir = runtime.expand(cfg.remote_repo_dir)
        print(f"Updating spark-lab engine dependencies on {runtime.label} ({repo_dir})...")
        _refresh_engine_deps(repo_dir, runtime)
    else:
        # This command is repo-centric: refresh the engine deps declared in the
        # checkout's pyproject.toml (no-op when run from an installed copy).
        repo_root = Path(__file__).resolve().parents[2]
        if (repo_root / "pyproject.toml").is_file():
            print("Updating spark-lab engine dependencies (pyproject + uv.lock)...")
            _refresh_engine_deps(repo_root, runtime)

    sparkrun = converge.find_sparkrun(runtime)
    home = runtime.home_path() if runtime is not None else None
    compose_file = cfg.node_path("litellm/docker-compose.yml", home)
    print("Updating sparkrun + recipe registries...")
    run_command([sparkrun, "update"], ok=True, runtime=runtime)
    print("Pulling latest stack images...")
    run_command(["docker", "compose", "-f", compose_file, "pull"], ok=True, runtime=runtime)
    print("Re-applying (model restart allowed)...")
    args.apply = True
    return apply_cmd.run(args)
