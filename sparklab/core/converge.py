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


def find_sparkrun(runtime=None) -> str:
    """Locate the `sparkrun` executable (it is often installed via `uv`).

    When ``runtime`` targets a remote node, resolution happens *on that node*
    (login-shell ``command -v``) instead of on the operator's machine.
    """
    if os.environ.get("SPARKRUN"):
        return os.environ["SPARKRUN"]
    if runtime is not None and getattr(runtime, "is_remote", False):
        located = runtime.locate("sparkrun")
        return located or "sparkrun"   # bare name: a clear error will surface if missing
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
        # commands whose failure is non-fatal (root-gated infra ensures); a failure
        # here warns + continues instead of aborting the whole converge.
        self.best_effort: set = set()
        # commands to launch *detached* (spawn, don't wait). The model launch lives
        # here: `sparkrun run` foreground-tails the model log and would otherwise
        # block the converge forever. A later bounded readiness probe confirms it.
        self.background: set = set()

    @property
    def any_change(self) -> bool:
        return bool(self.file_changes)


def _recipe_rel(recipe: str) -> str:
    return f"sparkrun/recipes/{recipe}.yaml"


def _model_readiness_probe(cfg) -> List[str]:
    """A bounded shell command that polls the model ``/health`` until ready.

    Replaces the model's foreground log-tail as the "is it up?" signal: a bounded
    poll (default ~10 min) means a failed model start is surfaced as an apply
    failure instead of the converge hanging. Probes the first host; refining the
    per-model primary endpoint for multi-node clusters is a follow-up
    (see docs/CLUSTERING.md).
    """
    host = str(cfg.hosts[0]) if cfg.hosts else "127.0.0.1"
    port = int(cfg.model.get("port", 30000))
    url = f"http://{host}:{port}/health"
    polls, sleep_s = 120, 5
    loop = (
        f"for i in $(seq 1 {polls}); do "
        f"curl -fsS -m 5 {url} >/dev/null 2>&1 && exit 0; "
        f"sleep {sleep_s}; done; "
        f"echo 'model not ready after {polls * sleep_s}s ({url})' >&2; exit 1"
    )
    return ["sh", "-c", loop]


def _install_rel_path(cfg, rel: str, home: Optional[str]) -> str:
    """A path under the install dir, as it exists on the target node.

    Real ``Config`` objects know how (``node_path``: local expansion, or remote
    expansion against the remote home). Test fakes that only provide
    ``install_dir`` fall back to the historical local computation.
    """
    node_path = getattr(cfg, "node_path", None)
    if callable(node_path):
        return node_path(rel, home)
    return str(Path(str(cfg.install_dir)) / rel)


def build_plan(cfg, rendered: dict, state_files: dict, state_model, allow_restart: bool,
               runtime=None) -> Plan:
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

    sparkrun = find_sparkrun(runtime)
    # Node-side paths: local mode resolves on this machine (byte-identical to the
    # historical behavior); remote mode resolves against the remote $HOME so the
    # plan carries absolute node paths (no `~` to re-expand inside quoted argv).
    home = runtime.home_path() if runtime is not None else None
    compose_file = _install_rel_path(cfg, "litellm/docker-compose.yml", home)
    # Run by the recipe's file path: sparkrun's bare-name lookup searches its
    # registries, not our install dir. The path is always present + unambiguous.
    recipe_file = _install_rel_path(cfg, f"sparkrun/recipes/{cfg.recipe_name}.yaml", home)
    cluster = cfg.is_cluster
    hosts = ",".join(str(h) for h in cfg.hosts)

    def _host_flag() -> List[str]:
        # sparkrun needs host targeting even for a single node (--hosts);
        # a named cluster supplies its own hosts (--cluster).
        return ["--cluster", cfg.cluster_name] if cluster else ["--hosts", hosts]

    def stop_model(name: str) -> None:
        recipe_path = _install_rel_path(cfg, f"sparkrun/recipes/{name}.yaml", home)
        argv = [sparkrun, "stop", recipe_path] + _host_flag()
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

    plan.model_restart_pending = (not allow_restart) and (bool(stale_recipes) or restart_current)

    # --- litellm + monitoring stack -----------------------------------------
    # Brought up FIRST so the control plane (gateway + observability) is available
    # while the model is still loading -- the model launch below is detached and
    # does not block the converge.
    if litellm_touched:
        plan.commands.append(
            ("Reconcile LiteLLM + monitoring stack (up + remove orphans)",
             ["docker", "compose", "-f", compose_file, "up", "-d", "--remove-orphans"]))
    else:
        plan.notes.append("LiteLLM stack unchanged (skipping `docker compose up`).")

    # --- model workload -------------------------------------------------------
    # Launch detached (`--ensure` is idempotent: no-op if already up). `sparkrun run`
    # otherwise foreground-tails the model log and would block the converge forever
    # (so the control plane above would never start on a fresh apply). A bounded
    # readiness probe (next) confirms the model actually came up instead of the
    # log-tail, so a failed start is surfaced rather than silently hung.
    if has_model:
        model_desc = "Start/ensure model workload (detached)"
        plan.commands.append(
            (model_desc, [sparkrun, "run", recipe_file, "--ensure"] + _host_flag()))
        plan.background.add(model_desc)
        plan.commands.append(
            ("Wait for model to be ready (bounded)", _model_readiness_probe(cfg)))

    # --- network ------------------------------------------------------------
    if cfg.tailscale().get("enabled", True):
        ts_desc = "Ensure Tailscale is enabled + running"
        plan.commands.append((ts_desc, ["systemctl", "enable", "--now", "tailscaled"]))
        plan.best_effort.add(ts_desc)   # needs root; a denial shouldn't abort the converge
    if cfg.cloudflare().get("enabled", False):
        cf_desc = "Ensure Cloudflare Tunnel is running"
        plan.commands.append((cf_desc, ["systemctl", "enable", "--now", "cloudflared"]))
        plan.best_effort.add(cf_desc)

    return plan


def write_files(cfg, rendered: dict, dry_run: bool, fs=None) -> List[str]:
    """Copy rendered files into the target node's install dir. Returns written paths.

    ``fs`` is the install-dir seam (see ``sparklab.core.node``); it defaults to
    the local install dir, keeping local behavior byte-identical.
    """
    if fs is None:
        from . import node as node_mod
        fs = node_mod.LocalInstallFS(Path(cfg.install_dir))
    written: List[str] = []
    for rel, data in rendered.items():
        if dry_run:
            continue
        written.append(fs.write(rel, data))
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
        if desc in getattr(plan, "background", set()):
            # Launch detached: don't block the converge on this process (the model
            # log-tail would run until the model exits). A bounded readiness probe
            # later confirms it actually came up.
            runtime.spawn(argv)
            continue
        result = runtime.run(argv)
        if result.returncode != 0:
            if desc in getattr(plan, "best_effort", set()):
                print(f"!! (best-effort, continuing) {desc} returned {result.returncode} "
                      f"(usually needs root; the rest of the converge still applies)")
                continue
            print(f"!! command failed ({result.returncode}): {' '.join(map(str, argv))}")
            exit_code = result.returncode
            break
    return exit_code
