"""Persistent, per-file hash state so `apply` converges instead of re-doing work.

State lives in ``.sparklab-state/state.json`` (gitignored). It maps each target
file (relative to ``install_dir``) to the sha256 of the content last applied.
Re-running ``apply`` only re-acts on files whose hash changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class State:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.state_dir / "state.json"

    def load(self) -> dict:
        if self._file.is_file():
            try:
                return json.loads(self._file.read_text())
            except json.JSONDecodeError:
                return {"files": {}}
        return {"files": {}}

    def save(self, data: dict) -> None:
        self._file.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    @property
    def files(self) -> dict:
        return self.load().get("files", {})

    def set_files(self, files: dict) -> None:
        self.save({"files": files})
