"""Persistent state so `apply` converges instead of re-doing work.

State lives in ``.sparklab-state/state.json`` (gitignored):

    {
      "files": { "<rel>": "<sha256>", ... },
      "model": { "name": "<recipe>", "hash": "<sha256 of that recipe>" }   # optional
    }

``files`` tracks which rendered files have been written to the install dir.
``model`` tracks the recipe the model is *confirmed running* with. These are
deliberately separate: files-on-disk and model-running are different things, and
tracking them separately is what lets a "recipe changed but not restarted" state
stay visibly dirty instead of silently drifting.
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

    @property
    def model(self):
        """The recipe the model is confirmed running with, or None."""
        return self.load().get("model")

    def set_state(self, files: dict, model) -> None:
        data = {"files": files}
        if model:
            data["model"] = model
        self.save(data)

    def clear(self) -> None:
        """Remove the state file (e.g. after a teardown) so the next apply
        converges from scratch instead of assuming an already-running stack."""
        if self._file.is_file():
            self._file.unlink()
