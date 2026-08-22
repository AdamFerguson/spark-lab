"""`spark-lab apply` — render + converge to config (idempotent)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from ..core import config, converge, render, state


def run(args) -> int:
    cfg = config.load(args.config)
    dry = getattr(args, "dry_run", False)
    allow_restart = bool(getattr(args, "apply", False) or getattr(args, "yes", False))
    runtime = getattr(args, "runtime", None)

    print(f"== spark-lab apply {'[dry-run]' if dry else ''} ==")
    print(f"   config     : {cfg.config_path}")
    print(f"   install dir: {cfg.install_dir}")
    print(f"   hosts      : {', '.join(str(h) for h in cfg.hosts)}"
          f"{'  (cluster)' if cfg.is_cluster else ''}")

    # Dry-run renders into a throwaway dir so it truly writes nothing to the repo.
    out_dir = Path(tempfile.mkdtemp(prefix="sparklab-dry-")) if dry else cfg.deploy_dir
    rendered = render.render(cfg, out_dir)
    st = state.State(cfg.state_dir)
    plan = converge.build_plan(cfg, rendered, st.files, st.model, allow_restart)

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
        print("\n[dry-run] No files written, no commands executed.")
        return 0

    written = converge.write_files(cfg, rendered, dry_run=False)
    if written:
        print(f"\nWrote {len(written)} file(s) to {cfg.install_dir}")
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
                  "Re-run with `spark-lab apply --apply` to restart the model.")
    return rc
