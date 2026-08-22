"""`spark-lab upgrade` — update sparkrun + images, re-apply."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..core import config, converge
from ..util import run_command
from . import apply as apply_cmd


def _refresh_engine_deps(repo_root: Path, runtime):
    """Refresh the engine deps. uv-first (re-resolve the lock + re-sync the
    runtime env); fall back to `pip install -U -e .` when uv is absent. uv
    detection routes through the runtime seam so it is testable."""
    have_uv = bool(runtime) and runtime.available("uv")
    if not have_uv and runtime is None:
        have_uv = shutil.which("uv") is not None
    if have_uv:
        run_command(["uv", "lock", "--upgrade", "--directory", str(repo_root)], ok=True, runtime=runtime)
        run_command(["uv", "sync", "--no-default-groups", "--directory", str(repo_root)], ok=True, runtime=runtime)
    else:
        run_command([sys.executable, "-m", "pip", "install", "-U", "-e", str(repo_root)], ok=True, runtime=runtime)


def run(args) -> int:
    cfg = config.load(args.config)
    runtime = getattr(args, "runtime", None)
    # This command is repo-centric: refresh the engine deps declared in the
    # checkout's pyproject.toml (no-op when run from an installed copy).
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "pyproject.toml").is_file():
        print("Updating spark-lab engine dependencies (pyproject + uv.lock)...")
        _refresh_engine_deps(repo_root, runtime)
    sparkrun = converge.find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    print("Updating sparkrun + recipe registries...")
    run_command([sparkrun, "update"], ok=True, runtime=runtime)
    print("Pulling latest stack images...")
    run_command(["docker", "compose", "-f", compose_file, "pull"], ok=True, runtime=runtime)
    print("Re-applying (model restart allowed)...")
    args.apply = True
    return apply_cmd.run(args)
