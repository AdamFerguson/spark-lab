"""Converge a node (or cluster) to match the rendered config.

`apply` never "re-does" work: it diffs the rendered files against the recorded
state and only re-acts on what changed. Destructive actions (restarting the
model, recreating the stack) are gated behind an explicit opt-in so a routine
`apply` is always safe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List

from . import state as state_mod


def find_sparkrun() -> str:
    """Locate the `sparkrun` executable (it is often installed via `uv`)."""
    if os.environ.get("SPARKRUN"):
        return os.environ["SPARKRUN"]
    which = shutil.which("sparkrun")
    if which:
        return which
    local = Path.home() / ".local" / "bin" / "sparkrun"
    if local.exists():
        return str(local)
    return "sparkrun"  # fall back to PATH; a clear error will surface if missing


class Plan:
    def __init__(self) -> None:
        self.file_changes: List[tuple] = []   # (target_rel, "added" | "changed")
        self.commands: List[tuple] = []       # (description, [argv])
        self.notes: List[str] = []

    @property
    def any_change(self) -> bool:
        return bool(self.file_changes)


def build_plan(cfg, rendered: dict, state_files: dict, allow_restart: bool) -> Plan:
    plan = Plan()
    for rel, data in rendered.items():
        h = state_mod.sha256_bytes(data)
        if state_files.get(rel) != h:
            plan.file_changes.append((rel, "added" if rel not in state_files else "changed"))

    changed = {rel for rel, _ in plan.file_changes}
    litellm_changed = any(r.startswith("litellm/") for r in changed)
    recipe_changed = any(r.startswith("sparkrun/") for r in changed)

    sparkrun = find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    cluster = cfg.is_cluster
    hosts = ",".join(str(h) for h in cfg.hosts)

    # --- model workload ----------------------------------------------------
    if cluster:
        plan.commands.append(
            ("Set up passwordless SSH mesh across hosts",
             [sparkrun, "setup", "ssh", "--hosts", hosts]))
        plan.commands.append(
            ("Create the saved cluster (use `spark-lab` once; switch to "
             "`sparkrun cluster update` if it already exists)",
             [sparkrun, "cluster", "create", cfg.cluster_name, "--hosts", hosts]))
        run_argv = [sparkrun, "run", cfg.recipe_name, "--cluster", cfg.cluster_name]
        if recipe_changed and allow_restart:
            plan.commands.append(("Stop model workload",
                                  [sparkrun, "stop", cfg.recipe_name, "--cluster", cfg.cluster_name]))
        plan.commands.append(("Start/ensure model workload", run_argv + ["--ensure"]))
    else:
        if recipe_changed and allow_restart:
            plan.commands.append(("Stop model workload", [sparkrun, "stop", cfg.recipe_name]))
        ensure = [sparkrun, "run", cfg.recipe_name, "--ensure"]
        if recipe_changed and not allow_restart:
            plan.notes.append(
                "Recipe changed but restart not requested. Re-run with "
                "`spark-lab apply --apply` to restart the model with the new recipe.")
        plan.commands.append(("Start/ensure model workload (no-op if already up)", ensure))

    # --- litellm + monitoring stack ----------------------------------------
    if litellm_changed:
        plan.commands.append(
            ("Start/recreate the LiteLLM + monitoring stack",
             ["docker", "compose", "-f", compose_file, "up", "-d"]))
    else:
        plan.notes.append("LiteLLM stack unchanged (skipping `docker compose up`).")

    # --- network -----------------------------------------------------------
    if cfg.tailscale().get("enabled", True):
        plan.commands.append(("Ensure Tailscale is enabled + running",
                              ["systemctl", "enable", "--now", "tailscaled"]))
    if cfg.cloudflare().get("enabled", False):
        plan.commands.append(("Ensure Cloudflare Tunnel is running",
                              ["systemctl", "enable", "--now", "cloudflared"]))

    return plan


def write_files(cfg, rendered: dict, dry_run: bool) -> List[str]:
    """Copy rendered files into the on-node install dir. Returns written paths."""
    written: List[str] = []
    base = Path(cfg.install_dir)
    for rel, data in rendered.items():
        dest = base / rel
        if dry_run:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written.append(str(dest))
    return written


def execute(plan: Plan, dry_run: bool, verbose: bool = True) -> int:
    """Run the plan's commands. In dry-run mode, only print them."""
    exit_code = 0
    for desc, argv in plan.commands:
        prefix = "[dry-run] would run: " if dry_run else "==> "
        if verbose:
            print(f"{prefix}{desc}")
            print(f"        {' '.join(map(str, argv))}")
        if dry_run:
            continue
        result = subprocess.run(argv)
        if result.returncode != 0:
            print(f"!! command failed ({result.returncode}): {' '.join(map(str, argv))}")
            exit_code = result.returncode
            break
    return exit_code
