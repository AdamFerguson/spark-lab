"""`spark-lab apply` — render + converge to config (idempotent), per host.

Host-targeted (ADR 0008): runs the full converge once per selected host
(``--hosts``), each against that host's config view + runtime + state. One
host's failure does not stop the others; the exit code reflects any failure.
"""
from __future__ import annotations

import difflib
import sys
import tempfile
from pathlib import Path

from ..core import cluster, config, converge, render


def _print_diffs(cfg, rendered, file_changes, fs, limit=200):
    """Show a unified diff vs the current on-disk install for each change."""
    print("\n--- diff vs current install (a/ = on disk, b/ = would write) ---")
    for rel, kind in file_changes:
        new = rendered.get(rel, b"").decode("utf-8", "replace")
        old_bytes = fs.read(rel)
        old = old_bytes.decode("utf-8", "replace") if old_bytes else ""
        lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                          fromfile=f"a/{rel}", tofile=f"b/{rel}",
                                          lineterm=""))
        if not lines:
            continue
        print(f"\n### {kind.upper()}: {rel}")
        print("\n".join(lines[:limit]))


def _converge_one(t, dry: bool, allow_restart: bool, diff: bool) -> int:
    """The converge for one host (the historical single-target apply body)."""
    cfg, runtime = t.cfg, t.runtime
    # fail-safe (ADR 0004): a model with no image can't be served -- refuse early
    # rather than converge on an unresolvable image. (Hosts with no active model
    # converge control-plane only and skip these checks entirely.)
    if cfg.model and not cfg.image_model():
        print(f"[ERROR] no image for active model '{cfg.active_alias}' "
              f"(set models.<alias>.image or SPARKLAB_IMAGE_MODEL).", file=sys.stderr)
        return 1
    if cfg.model and int(cfg.model.get("min_nodes", 1)) > 1:
        print(f"[ERROR] multi-node (TP) model placement is not supported yet "
              f"(ADR 0007 follow-up); this host would serve "
              f"'{cfg.active_alias}' with min_nodes={cfg.model.get('min_nodes')}.",
              file=sys.stderr)
        return 1

    print(f"   install dir: {cfg.install_dir_raw if t.is_remote else cfg.install_dir}"
          f"{' (on the node)' if t.is_remote else ''}")

    # Dry-run renders into a throwaway dir so it truly writes nothing to the repo.
    out_dir = Path(tempfile.mkdtemp(prefix="sparklab-dry-")) if dry else cfg.deploy_dir
    rendered = render.render(cfg, out_dir)
    fs, st = t.env()
    plan = converge.build_plan(cfg, rendered, st.files, st.model, allow_restart,
                               runtime=runtime)

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
        if diff:
            _print_diffs(cfg, rendered, plan.file_changes, fs)
        print("\n[dry-run] No files written, no commands executed.")
        return 0

    written = converge.write_files(cfg, rendered, dry_run=False, fs=fs)
    if written:
        where = f" on {runtime.label}" if t.is_remote else f" to {cfg.install_dir}"
        print(f"\nWrote {len(written)} file(s){where}")
    rc = converge.execute(plan, dry_run=False, runtime=runtime)
    if rc == 0:
        new_files = converge.compute_files_after_apply(rendered)
        has_model = plan.current_hash is not None
        converged_after = (not plan.model_restart_pending) if has_model else allow_restart
        new_model = converge.compute_model_after_apply(
            st.model, cfg.recipe_name, plan.current_hash, converged_after)
        st.set_state(new_files, new_model)
        print("\nConverged. State updated.")
        if plan.model_restart_pending:
            print("NOTE: a model change is still pending (not restarted). "
                  "Re-run with `spark-lab apply --restart-model` to restart the model.")
    return rc


def run(args) -> int:
    cfg = config.load(args.config)
    dry = getattr(args, "dry_run", False)
    allow_restart = bool(getattr(args, "restart_model", False)
                         or getattr(args, "apply", False) or getattr(args, "yes", False))
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        print("No hosts selected.", file=sys.stderr)
        return 1

    print(f"== spark-lab apply {'[dry-run]' if dry else ''} ==")
    print(f"   config : {cfg.config_path}")
    print(f"   hosts  : {', '.join(t.name for t in ts)}")

    return cluster.run_on_each(ts, lambda t: _converge_one(t, dry, allow_restart,
                                                           getattr(args, "diff", False)))
