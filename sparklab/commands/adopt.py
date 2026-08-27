"""`spark-lab adopt` -- take over an existing, already-running install without
disturbing it (per selected host).

Adoption is **read-only against the live install dir**: it renders what spark-lab
would write, compares each file against what is actually on disk, and records the
*on-disk* reality into the host's ``.sparklab-state/state.json``. It writes
**only** the state file (never into the install dir) and runs **no**
``sparkrun`` command, so a running model is left untouched.

After adoption, ``status`` reflects the live install and a routine ``apply`` will
NOT restart the model (the restart is fail-safe gated behind
``--restart-model``). Drift between the live files and what spark-lab would
render is reported, so you can decide whether to converge it (one-time model
restart) or leave the live files as-is.

In remote mode the same read-only adoption runs against the remote node, and
the state file is written *on the managed node* (in its spark-lab checkout) so
node-local and remote operation share one source of truth.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..core import cluster, config, node, render, state


def _adopt_one(t, dry: bool) -> int:
    cfg = t.cfg
    runtime = t.runtime
    fs, st = t.env()
    install_desc = cfg.install_dir_raw if t.is_remote else str(cfg.install_dir)
    if not fs.base_exists():
        print(f"[ERROR] install dir not found: {install_desc}", file=sys.stderr)
        print("Adoption assumes an already-installed (running) setup. Point the config's",
              file=sys.stderr)
        print("install.install_dir at it, or run `spark-lab init` + `apply` first.",
              file=sys.stderr)
        return 1

    rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-adopt-")))

    files: dict = {}
    adopted: list = []
    drift: list = []
    missing: list = []
    for rel, data in rendered.items():
        rendered_h = state.sha256_bytes(data)
        on_disk = fs.read(rel)
        if on_disk is not None:
            on_disk_h = state.sha256_bytes(on_disk)
            files[rel] = on_disk_h          # adopt on-disk reality
            (adopted if on_disk_h == rendered_h else drift).append(rel)
        else:
            missing.append(rel)            # not on disk; left out so next `apply` adds it

    # --- model: adopt the on-disk active recipe (so a routine apply won't restart) ---
    model = None
    recipe_rel = f"sparkrun/recipes/{cfg.recipe_name}.yaml"
    recipes_on_disk = fs.list_recipes()
    if cfg.recipe_name:
        recipe_bytes = fs.read(recipe_rel)
        if recipe_bytes is not None:
            model = {"name": cfg.recipe_name, "hash": state.sha256_bytes(recipe_bytes)}
        else:
            sys.stderr.write(
                f"\nNOTE: active recipe '{cfg.recipe_name}' not found in "
                f"{install_desc}/sparkrun/recipes. "
                f"Recipes on disk: {', '.join(recipes_on_disk) or '(none)'}\n"
                f"      Set the model's recipe name to one of those so the "
                f"running model is adopted. The model will be recorded as 'none' for now.\n")

    model_converged = bool(
        model and model["hash"] == state.sha256_bytes(rendered.get(recipe_rel, b"")))

    print(f"   install dir : {install_desc}{' (remote)' if t.is_remote else ''}")
    print(f"   active model: {cfg.recipe_name or '(none)'}")
    print(f"   files on disk that match the render : {len(adopted)}")
    print(f"   files on disk that DRIFT from render: {len(drift)}")
    for rel in drift:
        print(f"      - {rel}")
    print(f"   files missing from the live install : {len(missing)}")
    for rel in missing:
        print(f"      - {rel}   (next `apply` would add it)")
    if model:
        print(f"   model adopted           : {model['name']}"
              f"{' (converged: matches the render)' if model_converged else ' (drifts from the render)'}")
    else:
        print("   model adopted           : none")

    print("\nAdoption is read-only against the live install; it writes only the state file.")
    if dry:
        print("[dry-run] No state written.")
        return 0

    st.set_state(files, model)
    if t.is_remote:
        print("Wrote the state file on the target node.")
    else:
        print(f"Wrote {st._file}")
    print("\nNext steps:")
    print("  1. `spark-lab status`            -- confirm the live stack reads back.")
    print("  2. `spark-lab apply --dry-run`   -- preview any drift to converge; a routine apply will")
    print("                                      NOT restart the model (fail-safe).")
    if drift:
        print("  3. To converge the drifted files (one-time model restart), run "
              "`apply --restart-model`")
        print("     deliberately; otherwise the live files stay as they are.")
    return 0


def run(args) -> int:
    cfg = config.load(args.config)
    dry = getattr(args, "dry_run", False)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    print(f"== spark-lab adopt {'[dry-run]' if dry else ''} ==")
    print(f"   hosts : {', '.join(t.name for t in ts)}")
    return cluster.run_on_each(ts, lambda t: _adopt_one(t, dry))
