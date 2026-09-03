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
until `apply --restart-model` actually restarts the model — it does not silently drift.
"""

from __future__ import annotations

import os
import shlex
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
        return located or "sparkrun"  # bare name: a clear error will surface if missing
    which = shutil.which("sparkrun")
    if which:
        return which
    local = Path.home() / ".local" / "bin" / "sparkrun"
    if local.exists():
        return str(local)
    return "sparkrun"  # fall back to PATH; a clear error will surface if missing


class Plan:
    def __init__(self) -> None:
        self.file_changes: List[tuple] = []  # (target_rel, "added" | "changed" | "removed" | "kept")
        self.commands: List[tuple] = []  # (description, [argv])
        self.notes: List[str] = []
        # model convergence bookkeeping
        self.model_converged: bool = False  # was the model already on the current recipe?
        self.model_restart_pending: bool = False  # a model restart is needed but was not requested
        self.current_hash: Optional[str] = None  # sha256 of the current rendered recipe (None if no model)
        self.recipe_name: Optional[str] = None
        # commands whose failure is non-fatal (root-gated infra ensures); a failure
        # here warns + continues instead of aborting the whole converge.
        self.best_effort: set = set()
        # commands to launch *detached* (spawn, don't wait). The model launch lives
        # here: `sparkrun run` foreground-tails the model log and would otherwise
        # block the converge forever. A later bounded readiness probe confirms it.
        self.background: set = set()
        # node-side bookkeeping for the detached model launch: the PID file the
        # launch wrapper writes (so the probe can tell "still starting" from
        # "crashed") and the launch log (tailed when the probe fails).
        self.model_launch: Optional[Dict[str, str]] = None

    @property
    def any_change(self) -> bool:
        return bool(self.file_changes)


def _recipe_rel(recipe: str) -> str:
    return f"sparkrun/recipes/{recipe}.yaml"


def tolerant_stop_argv(sparkrun: str, recipe_path: str, host_flag: List[str]) -> List[str]:
    """argv for `sparkrun stop` that treats *nothing was running* as success.

    `sparkrun stop` exits non-zero when no running workload matches the
    recipe's intent ("No running workload matches intent ..."). That is the
    ALREADY-CONVERGED outcome -- nothing left to stop (the model was stopped
    out-of-band, never actually started, or the node rebooted) -- so the
    command captures the stop's output, re-echoes it to the terminal, and
    exits 0 for that specific outcome only. Every other failure (ambiguous
    workload, docker/ssh errors, ...) still fails the converge.
    """
    inner = " ".join(shlex.quote(str(x)) for x in [sparkrun, "stop", recipe_path] + list(host_flag))
    script = (
        f"out=$({inner} 2>&1); rc=$?; "
        f"printf '%s\\n' \"$out\"; "
        f"if [ $rc -eq 0 ]; then exit 0; fi; "
        f"if printf '%s' \"$out\" | grep -q 'No running workload matches intent'; then "
        f"echo '   (no running workload matched -- already stopped; continuing)'; "
        f"exit 0; fi; "
        f"exit 1"
    )
    return ["sh", "-c", script]


def _model_readiness_probe(cfg, pidfile: Optional[str] = None, logfile: Optional[str] = None) -> List[str]:
    """A bounded shell command that polls the model ``/health`` until ready.

    Replaces the model's foreground log-tail as the "is it up?" signal: a bounded
    poll (default ~10 min) means a failed model start is surfaced as an apply
    failure instead of the converge hanging. The bound is ``model.readiness_seconds``
    (default 600): slow-first-boot recipes (e.g. a 48 GB PLE table fill on
    Qwen3.8-Flash-Next) raise it. Probes the first host; refining the
    per-model primary endpoint for multi-node clusters is a follow-up
    (see docs/CLUSTERING.md).

    Crash detection: when the launch PID file is known, each cycle also checks
    whether the detached launch process is still alive. A start that crashed
    (the container exits, ``/health`` never comes up) then fails fast -- with
    the launch log tailed -- instead of polling a dead port for the whole
    bound. Two consecutive dead observations are required so a no-op
    ``--ensure`` (model already up) that exits promptly is not mistaken for a
    crash when ``/health`` is momentarily slow.
    """
    host = str(cfg.hosts[0]) if cfg.hosts else "127.0.0.1"
    port = int(cfg.model.get("port", 30000))
    seconds = int(cfg.model.get("readiness_seconds", 600))
    sleep_s = 5
    polls = max(1, seconds // sleep_s)
    url = f"http://{host}:{port}/health"
    loop = f"deads=0; for i in $(seq 1 {polls}); do curl -fsS -m 5 {url} >/dev/null 2>&1 && exit 0; "
    if pidfile:
        loop += (
            f"if [ -f {shlex.quote(pidfile)} ] && "
            f'! kill -0 "$(cat {shlex.quote(pidfile)} 2>/dev/null)" 2>/dev/null; then '
            "deads=$((deads + 1)); "
            'if [ "$deads" -ge 2 ]; then '
            'echo "model launch process exited before the model was ready" >&2; '
            + (f"tail -n 40 {shlex.quote(logfile)} >&2; " if logfile else "")
            + "exit 1; fi; fi; "
        )
    loop += (
        f"sleep {sleep_s}; done; "
        f"echo 'model not ready after {polls * sleep_s}s ({url})' >&2; "
        + (f"tail -n 40 {shlex.quote(logfile)} >&2; " if logfile else "")
        + "exit 1"
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


def gateway_health_argv(port) -> list:
    """Bounded liveness poll for the gateway (60s): used by converge after a
    planned restart and by `spark-lab litellm restart`."""
    return [
        "sh",
        "-c",
        "for i in $(seq 1 30); do "
        f"curl -fsS -o /dev/null http://127.0.0.1:{port}/health/liveliness"
        " && exit 0; sleep 2; done; exit 1",
    ]


def user_systemd_argv(inner: str) -> List[str]:
    """Run a ``systemctl --user`` command against the node user's manager.

    SSH (and fabric's login shell) do not always export XDG_RUNTIME_DIR; the
    explicit export makes user-service control work regardless (the unit must
    exist -- `spark-lab zoo prepare` installs it).
    """
    return ["sh", "-c", f"export XDG_RUNTIME_DIR=/run/user/$(id -u); {inner}"]


def swap_cmds(sparkrun: str, recipe_path: str, host_addr: str, gateway_name: str) -> Dict[str, str]:
    """The cmd/cmdStop strings llama-swap uses to start/stop one zoo engine.

    Both go through ``bash -lc`` so the login-shell PATH finds sparkrun (the
    user daemon does not inherit it). The stop reuses the EXACT tolerant
    pattern converge uses ("No running workload matches intent" == already
    stopped) so a TTL unload never trips on an already-gone workload. Paths use
    the ``{install_dir}`` placeholder (expanded at write time).
    """
    run = shlex.join([sparkrun, "run", recipe_path, "--ensure", "--hosts", host_addr])
    stop_script = tolerant_stop_argv(sparkrun, recipe_path, ["--hosts", host_addr])[-1]
    return {
        "cmd": "bash -lc " + shlex.quote(f"exec {run}"),
        "cmd_stop": "bash -lc " + shlex.quote(stop_script),
    }


def swap_cmds_script(kit_path: str, container: str, start_args=None, stop: str = "stop.sh") -> Dict[str, str]:
    """cmd/cmdStop for SCRIPT-mode zoo models (Mia-AiLab-style kit contracts).

    The kit's ``start.sh`` launches detached and exits once healthy, but
    llama-swap treats the cmd's lifetime as the server's -- so the cmd is a
    shim: resume-aware (attach if the container is already running, e.g. after
    a daemon restart), then block in ``docker wait`` for the container's real
    death. ``cmdStop`` is the kit's own idempotent ``stop.sh`` (which owns
    head+worker teardown for spanning kits); killing the shim alone can never
    orphan a container. ``kit_path`` may embed the ``{install_dir}`` placeholder.
    """
    start = "./start.sh" + (" " + " ".join(shlex.quote(str(a)) for a in (start_args or [])) if start_args else "")
    kitq = shlex.quote(kit_path)
    inner = (
        "C=" + shlex.quote(container) + "; "
        'if [ "$(docker inspect -f "{{.State.Running}}" "$C" 2>/dev/null)" = "true" ]; then '
        'echo "zoo: kit container already running -- attaching"; '
        "else cd " + kitq + " && " + start + " || exit 1; fi; "
        'exec docker wait "$C"'
    )
    return {
        "cmd": "bash -lc " + shlex.quote(inner),
        "cmd_stop": "bash -lc " + shlex.quote(f"cd {kitq} && ./" + shlex.quote(str(stop)) + " || true"),
    }


def build_plan(
    cfg, rendered: dict, state_files: dict, state_model, allow_restart: bool, runtime=None, launch_model: bool = True
) -> Plan:
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
        # A scaled-down recipe is deliberately LEFT on disk (unmanaged): with the
        # workload stopped the file is inert, re-scaling up just re-renders it, and
        # nothing in spark-lab picks recipes up by directory scan (the ensure/stop
        # paths always address an explicit path). All other managed files
        # (gateway/monitoring configs) are still deleted.
        kind = "kept" if rel.startswith("sparkrun/recipes/") else "removed"
        plan.file_changes.append((rel, kind))

    litellm_touched = any(r.startswith("litellm/") for r in changed) or any(r.startswith("litellm/") for r in removed)

    # --- model convergence --------------------------------------------------
    # A spanning model (min_nodes > 1) is a single workload that spans multiple
    # hosts; only its HEAD host (rank 0) owns the model's lifecycle. Worker
    # hosts skip the model bookkeeping entirely (they never launched it, so
    # there is nothing to converge or restart here).
    min_nodes = int(cfg.model.get("min_nodes", 1)) if cfg.model else 1
    spanning = has_model and min_nodes > 1
    # Placement is addressed the way sparkrun can resolve it (each host's
    # explicit ``ip:`` when set, else its name) -- its layout pins + ``--hosts``
    # match cluster host IPs, not hostnames.
    _addr = dict(getattr(cfg, "sparkrun_addresses", {}) or {})
    placement = [_addr.get(str(h), str(h)) for h in cfg.model_host_list(cfg.active_alias)] if spanning else []

    if launch_model:
        # Converged = the model is confirmed running the current recipe with the
        # current content. Anything else (new recipe, changed recipe, or removed)
        # needs a restart to converge.
        plan.model_converged = bool(
            has_model
            and state_model
            and state_model.get("name") == cfg.recipe_name
            and state_model.get("hash") == current_hash
        )

        # Model workloads that were previously managed/running but are no longer
        # the current recipe (switched away from, or removed): stop them to
        # converge.
        prev_recipes = {Path(r).stem for r in state_files if r.startswith("sparkrun/recipes/")}
        if state_model and state_model.get("name"):
            prev_recipes.add(state_model["name"])
        stale_recipes = sorted(prev_recipes - {cfg.recipe_name})

        # The current recipe needs a stop-then-start only if it was the running
        # one but its content changed (a brand-new recipe has nothing running
        # under it).
        restart_current = bool(
            state_model and state_model.get("name") == cfg.recipe_name and state_model.get("hash") != current_hash
        )
    else:
        # Worker host: the head owns the model; treat it as converged here so no
        # stop/restart is planned and no "needs restart" note is left pending.
        plan.model_converged = True
        stale_recipes = []
        restart_current = False

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
    hosts = ",".join(_addr.get(str(h), str(h)) for h in cfg.hosts)
    placement_flag = ",".join(placement)

    def _host_flag() -> List[str]:
        # sparkrun needs host targeting even for a single node (--hosts);
        # a named cluster supplies its own hosts (--cluster). A spanning model
        # targets exactly its own placement (its run pool IS models.<m>.hosts).
        if spanning:
            return ["--hosts", placement_flag]
        return ["--cluster", cfg.cluster_name] if cluster else ["--hosts", hosts]

    def stop_model(name: str) -> None:
        recipe_path = _install_rel_path(cfg, f"sparkrun/recipes/{name}.yaml", home)
        if allow_restart:
            # Tolerant of "no running workload" (already converged -- see
            # tolerant_stop_argv): a stale state entry for a model that is not
            # (anymore) running must not abort the converge.
            plan.commands.append(
                (f"Stop model workload {name}", tolerant_stop_argv(sparkrun, recipe_path, _host_flag()))
            )
        else:
            plan.notes.append(
                f"Model '{name}' needs to stop/restart, but that was not requested. "
                f"Re-run with `spark-lab apply --restart-model`."
            )

    if spanning and launch_model:
        # Build the passwordless SSH mesh from the head to its peers so the
        # spanning run can schedule the worker nodes. (No saved cluster: the run
        # targets the placement directly via --hosts.)
        plan.commands.append(
            ("Set up passwordless SSH mesh across hosts", [sparkrun, "setup", "ssh", "--hosts", placement_flag])
        )
    elif cluster:
        plan.commands.append(
            ("Set up passwordless SSH mesh across hosts", [sparkrun, "setup", "ssh", "--hosts", hosts])
        )
        plan.commands.append(
            ("Create the saved cluster", [sparkrun, "cluster", "create", cfg.cluster_name, "--hosts", hosts])
        )

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
            (
                "Reconcile LiteLLM + monitoring stack (up + remove orphans)",
                ["docker", "compose", "-f", compose_file, "up", "-d", "--remove-orphans"],
            )
        )
        # A bind-mounted config change is invisible to a RUNNING prometheus:
        # compose up only recreates services whose definition changed, and the
        # prometheus service definition does not when only prometheus.yml does.
        # Hot-reload it -- but only when the file was tracked before (a fresh
        # stack reads the file at boot). Best-effort: a briefly-down daemon
        # just misses the reload and picks the file up on its next start.
        if (
            "litellm/prometheus.yml" in state_files
            and "litellm/prometheus.yml" in changed
            and cfg.monitoring_role() == "full"
        ):
            prom_port = str(cfg.prometheus().get("port", 9090))
            reload_desc = "Reload prometheus config (hot, best-effort)"
            plan.commands.append(
                (
                    reload_desc,
                    ["curl", "-fsS", "-o", "/dev/null", "-X", "POST", f"http://127.0.0.1:{prom_port}/-/reload"],
                )
            )
            plan.best_effort.add(reload_desc)
        # Same bug class, gateway edition: a changed model list / gateway config
        # is invisible to a RUNNING litellm (it reads model_list at boot and
        # keeps it in its DB), so a model add/remove or an api_base flip would
        # sit inert until a manual restart. Any gateway-file change -- ADDED
        # (first extra_models entry!), changed, or removed -- restarts the
        # service whenever a previous converge is tracked (state non-empty:
        # the containers are already booted from older files, incl. after an
        # adopt). The very first apply needs nothing: compose up boots litellm
        # from the new files. Restart is best-effort, then VERIFIED: a restart
        # that does not come back healthy fails the converge loudly instead
        # of silently serving the old model list.
        gateway_files = [
            rel
            for rel in ("litellm/model_config.yaml", "litellm/config.yaml", "litellm/extra_models.yaml")
            if (rel in changed or rel in removed) and state_files
        ]
        if gateway_files and getattr(cfg, "control_plane_enabled", lambda: True)():
            restart_desc = "Restart litellm to apply the changed model list (best-effort)"
            plan.commands.append((restart_desc, ["docker", "compose", "-f", compose_file, "restart", "litellm"]))
            plan.best_effort.add(restart_desc)
            gw_port = cfg.litellm.get("port", 4000)
            plan.commands.append(("Verify the gateway came back healthy (bounded)", gateway_health_argv(gw_port)))
    else:
        plan.notes.append("LiteLLM stack unchanged (skipping `docker compose up`).")

    # --- model workload -------------------------------------------------------
    # Launch detached (`--ensure` is idempotent: no-op if already up). `sparkrun run`
    # otherwise foreground-tails the model log and would block the converge forever
    # (so the control plane above would never start on a fresh apply). A bounded
    # readiness probe (next) confirms the model actually came up instead of the
    # log-tail, so a failed start is surfaced rather than silently hung.
    if has_model and launch_model:
        model_desc = "Start/ensure model workload (detached)"
        # The launch wrapper records its own PID so the readiness probe can
        # distinguish "still starting" from "crashed": a dead launch process
        # fails the probe fast (with the launch log tailed) instead of polling
        # a dead port for the whole readiness bound. ``exec`` keeps the PID of
        # the `sh` wrapper when sparkrun replaces it.
        launch_argv = [sparkrun, "run", recipe_file, "--ensure"] + _host_flag()
        pidfile = "/tmp/sparklab-model-launch.pid"
        logfile = "/tmp/sparklab-model-launch.log"
        plan.model_launch = {"pidfile": pidfile, "logfile": logfile}
        plan.commands.append(
            (model_desc, ["sh", "-c", f"echo $$ > {shlex.quote(pidfile)}; exec " + shlex.join(launch_argv)])
        )
        plan.background.add(model_desc)
        plan.commands.append(
            ("Wait for model to be ready (bounded)", _model_readiness_probe(cfg, pidfile=pidfile, logfile=logfile))
        )
    elif has_model:
        plan.notes.append(
            f"Worker host for spanning model '{cfg.active_alias}': the workload "
            f"is launched by the head host (rank 0, {placement[0] if placement else '?'}); "
            f"nothing to launch here."
        )

    # --- network ------------------------------------------------------------
    if cfg.tailscale().get("enabled", True):
        ts_desc = "Ensure Tailscale is enabled + running"
        plan.commands.append((ts_desc, ["systemctl", "enable", "--now", "tailscaled"]))
        plan.best_effort.add(ts_desc)  # needs root; a denial shouldn't abort the converge
    if cfg.cloudflare().get("enabled", False):
        cf_desc = "Ensure Cloudflare Tunnel is running"
        plan.commands.append((cf_desc, ["systemctl", "enable", "--now", "cloudflared"]))
        plan.best_effort.add(cf_desc)

    # --- model swapping (zoo / llama-swap, ADR-0010) -------------------------
    # llama-swap runs as a USER systemd service on the zoo host; the unit is
    # installed by `spark-lab zoo prepare`, so every interaction is best-effort
    # (a missing unit must not abort an otherwise valid converge).
    if getattr(cfg, "swap_for_host", lambda: False)():
        swap_desc = "Ensure llama-swap service running (zoo)"
        plan.commands.append((swap_desc, user_systemd_argv("systemctl --user --no-block start llama-swap")))
        plan.best_effort.add(swap_desc)
        if "llama-swap/config.yaml" in changed and state_files:
            # The daemon reads its model config at start (and reloads on
            # change, but converge restarts deterministically so a partial
            # reload can never leave a half-applied zoo).
            rs_desc = "Restart llama-swap (zoo config changed)"
            plan.commands.append((rs_desc, user_systemd_argv("systemctl --user restart llama-swap")))
            plan.best_effort.add(rs_desc)
            v_desc = "Verify llama-swap is healthy"
            plan.commands.append(
                (
                    v_desc,
                    [
                        "sh",
                        "-c",
                        "for i in $(seq 1 15); do curl -fsS -o /dev/null http://127.0.0.1:"
                        f"{cfg.swap_port()}/health && exit 0; sleep 2; done; exit 1",
                    ],
                )
            )
            plan.best_effort.add(v_desc)
        plan.notes.append(
            "zoo active on this host: if llama-swap is not installed yet, run "
            "`spark-lab zoo prepare` (downloads stay a deliberate step)."
        )

    return plan


INSTALL_DIR_PLACEHOLDER = "{install_dir}"


def expand_install_dir(rel: str, data: bytes, base: Optional[str]) -> bytes:
    """Expand ``{install_dir}`` in a node-side sparkrun recipe.

    Recipes must stay reusable across machines, so host-side bind-mount
    sources are written as ``{install_dir}/...`` (e.g. the flash-next PLE
    volumes) instead of hardcoding ``/home/<you>/AI/...``. The concrete
    per-node install dir is only known at write time -- ``~/AI`` in the
    config refers to THAT node's filesystem, where the install FS has
    already expanded it. Applied to sparkrun recipe files only, right
    before they land in the install dir; a no-op otherwise (byte-identity
    for placeholder-free recipes is preserved).
    """
    if (
        base
        and (rel.startswith("sparkrun/recipes/") or rel.startswith("llama-swap/"))
        and INSTALL_DIR_PLACEHOLDER.encode() in data
    ):
        return data.replace(INSTALL_DIR_PLACEHOLDER.encode(), base.encode())
    return data


def write_files(cfg, rendered: dict, dry_run: bool, fs=None) -> List[str]:
    """Copy rendered files into the target node's install dir. Returns written paths.

    ``fs`` is the install-dir seam (see ``sparklab.core.node``); it defaults to
    the local install dir, keeping local behavior byte-identical.
    """
    if fs is None:
        from . import node as node_mod

        fs = node_mod.LocalInstallFS(Path(cfg.install_dir))
    base = str(getattr(fs, "base", "")) or None
    written: List[str] = []
    for rel, data in rendered.items():
        if dry_run:
            continue
        written.append(fs.write(rel, expand_install_dir(rel, data, base)))
    return written


def compute_files_after_apply(rendered: dict) -> Dict[str, str]:
    """File hashes to record after a successful apply.

    This is exactly the rendered set, so files that are no longer rendered are
    naturally dropped (removals converge).
    """
    return {rel: state_mod.sha256_bytes(data) for rel, data in rendered.items()}


def compute_model_after_apply(
    state_model, current_recipe: Optional[str], current_hash: Optional[str], converged_after: bool
):
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
            # log-tail would run until the model exits). The launch log is captured
            # so the readiness probe can tail it on failure. A bounded readiness
            # probe later confirms it actually came up.
            launch = getattr(plan, "model_launch", None)
            runtime.spawn(argv, log=(launch or {}).get("logfile"))
            continue
        result = runtime.run(argv)
        if result.returncode != 0:
            if desc in getattr(plan, "best_effort", set()):
                print(
                    f"!! (best-effort, continuing) {desc} returned {result.returncode} "
                    f"(usually needs root; the rest of the converge still applies)"
                )
                continue
            print(f"!! command failed ({result.returncode}): {' '.join(map(str, argv))}")
            exit_code = result.returncode
            break
    return exit_code
