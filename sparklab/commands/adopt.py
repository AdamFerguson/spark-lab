"""`spark-lab adopt` -- take over an existing, already-running install without
disturbing it.

Adoption is **read-only against the live install dir**: it renders what spark-lab
would write, compares each file against what is actually on disk, and records the
*on-disk* reality into ``.sparklab-state/state.json``. It writes **only** the
state file (never into the install dir) and runs **no** ``sparkrun`` command, so a
running model is left untouched.

After adoption, ``status`` reflects the live install and a routine ``apply`` will
NOT restart the model (the restart is fail-safe gated behind ``--apply``). Drift
between the live files and what spark-lab would render is reported, so you can
decide whether to converge it (one-time model restart via ``apply --apply``) or
leave the live files as-is.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..core import config, render, state


def _scan_recipes(install_dir: Path):
    """The recipe basenames actually present in the live install's recipes dir."""
    d = install_dir / "sparkrun" / "recipes"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def run(args) -> int:
    cfg = config.load(args.config)
    dry = getattr(args, "dry_run", False)
    install = Path(cfg.install_dir)
    if not install.is_dir():
        print(f"[ERROR] install dir not found: {install}", file=sys.stderr)
        print("Adoption assumes an already-installed (running) setup. Point the config's",
              file=sys.stderr)
        print(f"install.install_dir at it, or run `spark-lab init` + `apply` first.", file=sys.stderr)
        return 1

    rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-adopt-")))
    st = state.State(cfg.state_dir)

    files: dict = {}
    adopted: list = []
    drift: list = []
    missing: list = []
    for rel, data in rendered.items():
        dest = install / rel
        rendered_h = state.sha256_bytes(data)
        if dest.is_file():
            on_disk_h = state.sha256_bytes(dest.read_bytes())
            files[rel] = on_disk_h          # adopt on-disk reality
            (adopted if on_disk_h == rendered_h else drift).append(rel)
        else:
            missing.append(rel)            # not on disk; left out so next `apply` adds it

    # --- model: adopt the on-disk active recipe (so a routine apply won't restart) ---
    model = None
    recipe_rel = f"sparkrun/recipes/{cfg.recipe_name}.yaml"
    recipe_path = install / recipe_rel
    recipes_on_disk = _scan_recipes(install)
    if recipe_path.is_file():
        model = {"name": cfg.recipe_name, "hash": state.sha256_bytes(recipe_path.read_bytes())}
    else:
        sys.stderr.write(
            f"\nNOTE: active recipe '{cfg.recipe_name}' not found in {install/'sparkrun'/'recipes'}. "
            f"Recipes on disk: {', '.join(recipes_on_disk) or '(none)'}\n"
            f"      Set model.recipe_name (or the active model's) to one of those so the "
            f"running model is adopted. The model will be recorded as 'none' for now.\n")

    model_converged = bool(
        model and model["hash"] == state.sha256_bytes(rendered.get(recipe_rel, b"")))

    print(f"== spark-lab adopt {'[dry-run]' if dry else ''} ==")
    print(f"   install dir : {install}")
    print(f"   active model: {cfg.recipe_name}")
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
    print(f"Wrote {st._file}")
    print("\nNext steps:")
    print("  1. `spark-lab status`            -- confirm the live stack reads back.")
    print("  2. `spark-lab apply --dry-run`   -- preview any drift to converge; a routine apply will")
    print("                                      NOT restart the model (fail-safe).")
    if drift:
        print("  3. To converge the drifted files (one-time model restart), run `apply --apply`")
        print("     deliberately; otherwise the live files stay as they are.")
    return 0
