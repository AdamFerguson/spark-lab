"""`spark-lab upgrade` — update sparkrun + images, re-apply."""
from __future__ import annotations

import sys
from pathlib import Path

from ..core import config, converge
from ..util import run_command
from . import apply as apply_cmd


def run(args) -> int:
    cfg = config.load(args.config)
    runtime = getattr(args, "runtime", None)
    # This command is repo-centric: refresh the engine deps declared in the
    # checkout's requirements.txt (no-op when run from an installed copy).
    repo_root = Path(__file__).resolve().parents[2]
    req = repo_root / "requirements.txt"
    if req.is_file():
        print("Updating spark-lab engine dependencies...")
        run_command([sys.executable, "-m", "pip", "install", "-U", "-r", str(req)],
                    ok=True, runtime=runtime)
    sparkrun = converge.find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    print("Updating sparkrun + recipe registries...")
    run_command([sparkrun, "update"], ok=True, runtime=runtime)
    print("Pulling latest stack images...")
    run_command(["docker", "compose", "-f", compose_file, "pull"], ok=True, runtime=runtime)
    print("Re-applying (model restart allowed)...")
    args.apply = True
    return apply_cmd.run(args)
