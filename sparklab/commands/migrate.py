"""`spark-lab migrate` — rewrite a v1 config.yaml to schema v2 on disk.

Idempotent: a v2 config is left untouched. The rewrite preserves every value
(ADR 0004 ``upgrade_to_v2``) and adds ``version: 2``; because the transform is
render-invariant, a migrated config deploys identically to its v1 original.
Note: YAML comments are not preserved (a mechanical value-migration).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from ..core import schema

_HEADER = "# Migrated to schema v2 by `spark-lab migrate` (ADR 0004). Values preserved.\n"


def run(args) -> int:
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    if not cfg_path.is_file():
        print(f"No config found at {cfg_path}.", file=sys.stderr)
        return 1
    data = yaml.safe_load(cfg_path.read_text()) or {}
    if schema.is_v2(data):
        print(f"{cfg_path} is already schema v2; nothing to do.")
        return 0
    v2 = schema.upgrade_to_v2(data)
    text = _HEADER + yaml.safe_dump(v2, sort_keys=False, allow_unicode=True)
    if getattr(args, "dry_run", False):
        print("[dry-run] would write:")
        print(text)
        return 0
    cfg_path.write_text(text)
    print(f"Migrated {cfg_path} to v2 (models: {', '.join(v2['models'])}).")
    print("Verify with `spark-lab validate` and `spark-lab apply --dry-run`.")
    return 0
