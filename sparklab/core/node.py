"""The node file + state seam: local (path-based) or remote (over the runtime).

Commands never touch a local ``Path`` for the target node's install dir or its
state file; they obtain an install FS + state object from :func:`node_env`.
That single swap is what makes remote operator mode a matter of choosing the
backend, not re-plumbing every command.

* **Local** (no ``install.remote.host``): :class:`LocalInstallFS` over
  ``install.install_dir`` on this machine + :class:`~sparklab.core.state.State`
  in this checkout's ``.sparklab-state``.
* **Remote**: :class:`~sparklab.core.remote.RemoteInstallFS` /
  :class:`~sparklab.core.remote.RemoteState` over the runtime's SSH
  connection — the state file lives *on the managed node* (in its spark-lab
  checkout), so node-local and remote operation share one source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from . import state as state_mod


class LocalInstallFS:
    """File access to the install dir on this machine."""

    def __init__(self, base: Path):
        self.base = Path(base)

    def base_exists(self) -> bool:
        return self.base.is_dir()

    def path_str(self, rel: str) -> str:
        return str(self.base / rel)

    def exists(self, rel: str) -> bool:
        return Path(self.path_str(rel)).is_file()

    def read(self, rel: str) -> Optional[bytes]:
        p = Path(self.path_str(rel))
        return p.read_bytes() if p.is_file() else None

    def write(self, rel: str, data: bytes) -> str:
        p = Path(self.path_str(rel))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p)

    def hash_files(self, rels: List[str]) -> dict:
        out = {}
        for rel in rels:
            data = self.read(rel)
            out[rel] = (None if data is None else state_mod.sha256_bytes(data))
        return out

    def list_recipes(self) -> List[str]:
        d = self.base / "sparkrun" / "recipes"
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.yaml"))


def node_env(cfg, runtime) -> Tuple[object, object]:
    """Return ``(install_fs, state)`` for the target node of this config + runtime.

    Local when the runtime is not a remote one; otherwise both back the remote
    node over the runtime's SSH connection.
    """
    if getattr(runtime, "is_remote", False):
        from .remote import RemoteInstallFS, RemoteState
        return RemoteInstallFS(runtime), RemoteState(runtime)
    return LocalInstallFS(Path(cfg.install_dir)), state_mod.State(cfg.state_dir)
