"""Converge a node (or cluster) to match the rendered config.

`apply` never "re-does" work: it diffs the rendered files against the recorded
state and only re-acts on what changed. Destructive actions (restarting the
model) are gated behind an explicit opt-in so a routine `apply` is always safe.

Removals are first-class: a file that was managed before but is no longer in the
rendered set is planned as a "removed" change, and model workloads that are no
longer current are stopped (gated). Switching or dropping a recipe therefore
converges instead of leaving orphans running.

Files-on-disk and model-running are tracked separately (see sparklab.core.state). A recipe
change that has not been restarted therefore stays *pending* and keeps prompting
until `apply --apply` actually restarts the model — it does not silently drift.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

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
        self.file_changes: List[tuple] = []   # (target_rel, "added" | "changed" | "removed")
        self.commands: List[tuple] = []       # (description, [argv])
        self.notes: List[str] = []
        # model convergence bookkeeping
        self.model_converged: bool = False          # was the model already on the current recipe?
        self.model_restart_pending: bool = False    # a model restart is needed but was not requested
        self.current_hash: Optional[str] = None     # sha256 of the current rendered recipe (None if no model)
        self.recipe_name: Optional[str] = None

    @property
    def any_change(self) -> bool:
        return bool(self.file_changes)


def _recipe_rel(recipe: str) -> str:
    return f"sparkrun/recipes/{recipe}.yaml"


def build_plan(cfg, rendered: dict, state_files: dict, state_model, allow_restart: bool) -> Plan:
    plan = Plan()
    recipe_rel = _recipe_rel(cfg.recipe_name)
    has_model = recipe_rel in rendered
    current_hash = state_mod.sha256_bytes(rendered[recipe_rel]) if has_model else None
    plan.recipe_name = cfg.recipe_name if has_model else None
    plan.current_hash = current_hash

    # --- files: added / changed vs last apply -------------------------------
    added_changed = []
    for rel, data in rendered.items():
        h = state_mod.sha256_bytes(data)
        if state_files.get(rel) != h:
            added_changed.append((rel, "added" if rel not in state_files else "changed"))
    plan.file_changes.extend(added_changed)
    changed = {rel for rel, _ in added_changed}

    # --- files: removed (managed before, no longer rendered) ----------------
    removed = [rel for rel in state_files if rel not in rendered]
    for rel in removed:
        plan.file_changes.append((rel, "removed"))

    litellm_touched = (
        any(r.startswith("litellm/") for r in changed)
        or any(r.startswith("litellm/") for r in removed)
    )

    # --- model convergence --------------------------------------------------
    # Converged = the model is confirmed running the current recipe with the
    # current content. Anything else (new recipe, changed recipe, or removed)
    # needs a restart to converge.
    plan.model_converged = bool(
        has_model and state_model
        and state_model.get("name") == cfg.recipe_name
        and state_model.get("hash") == current_hash
    )
    needs_restart = has_model and not plan.model_converged

    # Model workloads that were previously managed/running but are no longer the
    # current recipe (switched away from, or removed): stop them to converge.
    prev_recipes = {Path(r).stem for r in state_files if r.startswith("sparkrun/recipes/")}
    if state_model and state_model.get("name"):
        prev_recipes.add(state_model["name"])
    stale_recipes = sorted(prev_recipes - {cfg.recipe_name})

    # The current recipe needs a stop-then-start only if it was the running one
    # but its content changed (a brand-new recipe has nothing running under it).
    restart_current = bool(
        state_model and state_model.get("name") == cfg.recipe_name
        and state_model.get("hash") != current_hash
    )

    sparkrun = find_sparkrun()
    compose_file = str(Path(cfg.install_dir) / "litellm" / "docker-compose.yml")
    cluster = cfg.is_cluster
    hosts = ",".join(str(h) for h in cfg.hosts)

    def _cluster_flag() -> List[str]:
        return ["--cluster", cfg.cluster_name] if cluster else []

    def stop_model(name: str) -> None:
        argv = [sparkrun, "stop", name] + _cluster_flag()
        if allow_restart:
            plan.commands.append((f"Stop model workload {name}", argv))
        else:
            plan.notes.append(
                f"Model '{name}' needs to stop/restart, but that was not requested. "
                f"Re-run with `spark-lab apply --apply`."
            )

    if cluster:
        plan.commands.append(("Set up passwordless SSH mesh across hosts",
                              [sparkrun, "setup", "ssh", "--hosts", hosts]))
        plan.commands.append(("Create the saved cluster",
                              [sparkrun, "cluster", "create", cfg.cluster_name, "--hosts", hosts]))

    # stop model workloads that are no longer current
    for name in stale_recipes:
        stop_model(name)
    # restart the running recipe if its definition changed
    if restart_current:
        stop_model(cfg.recipe_name)
    if has_model:
        plan.commands.append(
            ("Start/ensure model workload (no-op if already up)",
             [sparkrun, "run", cfg.recipe_name, "--ensure"] + _cluster_flag()))

    plan.model_restart_pending = (not allow_restart) and (bool(stale_recipes) or restart_current)

    # --- litellm + monitoring stack -----------------------------------------
    if litellm_touched:
        plan.commands.append(
            ("Reconcile LiteLLM + monitoring stack (up + remove orphans)",
             ["docker", "compose", "-f", compose_file, "up", "-d", "--remove-orphans"]))
    else:
        plan.notes.append("LiteLLM stack unchanged (skipping `docker compose up`).")

    # --- network ------------------------------------------------------------
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


def compute_files_after_apply(rendered: dict) -> Dict[str, str]:
    """File hashes to record after a successful apply.

    This is exactly the rendered set, so files that are no longer rendered are
    naturally dropped (removals converge).
    """
    return {rel: state_mod.sha256_bytes(data) for rel, data in rendered.items()}


def compute_model_after_apply(state_model, current_recipe: Optional[str],
                              current_hash: Optional[str], converged_after: bool):
    """Model entry to record after a successful apply.

    ``converged_after`` is True when, after this apply, the model is confirmed
    running the current recipe with the current content (or the model was
    intentionally removed). Record that; otherwise keep the previous entry so an
    un-restarted / un-stopped change stays visibly pending on the next apply.
    """
    if not converged_after:
        return state_model
    if current_hash is None:
        return None
    return {"name": current_recipe, "hash": current_hash}


def execute(plan: Plan, dry_run: bool, verbose: bool = True, runtime=None) -> int:
    """Run the plan's commands. In dry-run mode, only print them.

    ``runtime`` is the command<->runtime boundary (ADR 0002); it defaults to the
    real runtime. Tests pass a fake to capture the exact commands that would run.
    """
    if runtime is None:
        from . import runtime as runtime_mod
        runtime = runtime_mod.default_runtime()
    exit_code = 0
    for desc, argv in plan.commands:
        prefix = "[dry-run] would run: " if dry_run else "==> "
        if verbose:
            print(f"{prefix}{desc}")
            print(f"        {' '.join(map(str, argv))}")
        if dry_run:
            continue
        result = runtime.run(argv)
        if result.returncode != 0:
            print(f"!! command failed ({result.returncode}): {' '.join(map(str, argv))}")
            exit_code = result.returncode
            break
    return exit_code
