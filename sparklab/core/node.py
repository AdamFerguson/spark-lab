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

import os
from pathlib import Path
from typing import List, Optional, Tuple

from . import state as state_mod


def file_mode(rel: str) -> int:
    """Explicit mode for install files written by converge.

    SFTP-created files land as 0600 and local writes honor the process umask
    (077 on the Sparks) -- both unreadable by the container users of
    prometheus/grafana, which crash-loop on 0600 config files. So the mode is
    set explicitly: the rendered ``litellm/.env`` (carries secrets) stays 0600,
    everything else is world-readable 0644.
    """
    return 0o600 if rel.endswith(".env") else 0o644


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
        os.chmod(p, file_mode(rel))
        return str(p)

    def delete(self, rel: str) -> None:
        """Remove a managed file that no longer renders (best-effort)."""
        Path(self.path_str(rel)).unlink(missing_ok=True)

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
