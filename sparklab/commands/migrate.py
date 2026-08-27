"""`spark-lab migrate` — rewrite a v1/v2 config.yaml to schema v3 on disk.

Idempotent: a v3 config is left untouched. The rewrite preserves every value
(``schema.upgrade_to_v2`` + ``schema.upgrade_to_v3``) and adds ``version: 3``
with a single-entry ``hosts:`` list (this node -- and the remote target, when
the legacy config had ``install.remote``). Because the transforms preserve
values, a migrated config converges the same node the original did.
Note: YAML comments are not preserved (a mechanical value-migration).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from ..core import schema

_HEADER = ("# Migrated to schema v3 by `spark-lab migrate` (ADR 0008). Values preserved.\n"
           "# Multi-host: add more entries under `hosts:` (and `models.<m>.hosts`).\n")


def run(args) -> int:
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    if not cfg_path.is_file():
        print(f"No config found at {cfg_path}.", file=sys.stderr)
        return 1
    data = yaml.safe_load(cfg_path.read_text()) or {}
    if schema.is_v3(data):
        print(f"{cfg_path} is already schema v3; nothing to do.")
        return 0
    v3 = schema.upgrade_to_v3(data)
    text = _HEADER + yaml.safe_dump(v3, sort_keys=False, allow_unicode=True)
    if getattr(args, "dry_run", False):
        print("[dry-run] would write:")
        print(text)
        return 0
    cfg_path.write_text(text)
    hosts = ", ".join(h.get("name", "?") for h in v3["hosts"])
    print(f"Migrated {cfg_path} to v3 (hosts: {hosts}; models: {', '.join(v3.get('models', {}))}).")
    print("Verify with `spark-lab validate` and `spark-lab apply --dry-run`.")
    return 0
