"""Remote operator mode: run the converge on a remote node over SSH (Fabric).

This is the remote half of the ADR-0002 runtime seam. When a config sets
``install.remote.host``, the CLI builds a :class:`RemoteRuntime` instead of the
local one and everything else (render, plan, files, state) runs against the
remote node through this module. Local mode never imports it.

Design notes:

* **Login shell.** Every remote command runs as ``bash -lc '<cmd>'`` — a login
  shell — and we prepend ``$HOME/.local/bin`` + ``$HOME/.cargo/bin`` to ``PATH``
  explicitly. Non-interactive SSH otherwise misses the uv-managed ``~/.local/bin``
  (sparkrun, uv), which is the classic "sparkrun MISSING" first-run trap.
* **Tilde expansion happens on the node.** ``install_dir`` / ``repo_dir`` may be
  written as ``~/...``; we fetch the remote ``$HOME`` once and expand against it
  (never against the operator's machine).
* **Detached launch.** The model launch is wrapped in
  ``setsid nohup <cmd> </dev/null >/dev/null 2>&1 &`` so the SSH channel can
  close immediately while the model keeps loading; the bounded readiness probe
  (part of the converge plan) confirms it came up.
* **State stays on the node.** :class:`RemoteState` reads/writes
  ``<repo_dir>/.sparklab-state/state.json`` on the managed node — the single
  source of truth, regardless of which machine drives it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from typing import Dict, List, Optional

# Fabric is imported at module level, but this module is only imported lazily
# (see ``sparklab.core.runtime.runtime_for``) when a config has a remote target.
from fabric import Connection

from . import state as state_mod

# Standard PATH prefix for every remote command: uv tooling (~/.local/bin) and
# cargo installs (~/.cargo/bin) are where sparkrun/uv usually live on a Spark.
_PATH_PREFIX = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; '


def _login(inner: str) -> str:
    """Wrap ``inner`` so it executes in a remote login shell with a sane PATH."""
    return "bash -lc " + shlex.quote(_PATH_PREFIX + inner)


def _shell_argv(argv: List) -> str:
    """Join an argv list into a shell command (each element quoted)."""
    return " ".join(shlex.quote(str(a)) for a in argv)


class RemoteTarget:
    """Where to converge: the SSH endpoint + the node-side paths (raw form).

    ``install_dir`` / ``repo_dir`` are kept in their raw (possibly ``~``) form;
    they are expanded against the *remote* home by :class:`RemoteRuntime.expand`.
    """

    def __init__(self, host: str, user: Optional[str] = None, port: Optional[int] = None,
                 identity_file: Optional[str] = None,
                 install_dir: str = "~/AI", repo_dir: str = "~/spark-lab"):
        self.host = str(host)
        self.user = user
        self.port = int(port) if port else None
        self.identity_file = identity_file
        self.install_dir = str(install_dir)
        self.repo_dir = str(repo_dir)

    @classmethod
    def from_config(cls, cfg) -> "RemoteTarget":
        r = cfg.remote
        user = r.get("user") or os.environ.get("USER")
        return cls(
            host=str(r["host"]),
            user=user,
            port=r.get("port"),
            identity_file=r.get("identity_file"),
            install_dir=str(cfg.install.get("install_dir", "~/AI")),
            repo_dir=str(r.get("repo_dir") or "~/spark-lab"),
        )

    def __repr__(self) -> str:
        return (f"RemoteTarget(host={self.host!r}, user={self.user!r}, "
                f"install_dir={self.install_dir!r}, repo_dir={self.repo_dir!r})")


def build_connection(target: RemoteTarget) -> Connection:
    """A lazily-connecting Fabric ``Connection`` for the target node."""
    connect_kwargs: Dict = {}
    if target.identity_file:
        connect_kwargs["identity_filename"] = target.identity_file
    return Connection(target.host, user=target.user, port=target.port,
                      connect_kwargs=connect_kwargs or None)


class _Detached:
    """Stand-in for the ``Popen`` that the local ``Runtime.spawn`` returns.

    The process was launched on the remote node in a new session; we have no
    local handle to it (the bounded readiness probe is the liveness signal).
    """

    def __init__(self, argv: List, launch_rc: int):
        self.argv = list(argv)
        self.pid = None
        self.launch_rc = launch_rc


class RemoteRuntime:
    """The ADR-0002 runtime interface over one SSH connection (Fabric).

    Drop-in for :class:`~sparklab.core.runtime.Runtime`: ``available`` /
    ``run`` / ``spawn`` (plus ``locate`` and ``home_path`` used for node-side
    path resolution). One connection is reused for the whole spark-lab run.
    """

    is_remote = True

    def __init__(self, target: RemoteTarget, connection: Optional[Connection] = None):
        self.target = target
        self._conn = connection if connection is not None else build_connection(target)
        self._remote_home: Optional[str] = None

    # -- connection helpers --------------------------------------------------
    @property
    def conn(self) -> Connection:
        return self._conn

    def home_path(self) -> str:
        """The remote node's ``$HOME`` (fetched once, cached)."""
        if self._remote_home is None:
            r = self._conn.run(_login('echo "$HOME"'), warn=True)
            self._remote_home = r.stdout.strip() or "/"
        return self._remote_home

    def expand(self, path: str) -> str:
        """Expand ``~`` against the *remote* home (never the operator's).

        Only plain ``~`` / ``~/...`` forms are supported (the config surface
        uses those); anything else passes through unchanged.
        """
        p = str(path)
        if p == "~":
            return self.home_path()
        if p.startswith("~/"):
            return self.home_path() + p[1:]
        return p

    @property
    def label(self) -> str:
        user = self.target.user or "user"
        return f"{user}@{self.target.host}"

    # -- ADR-0002 runtime interface -------------------------------------------
    def locate(self, binary: str) -> Optional[str]:
        """The node's absolute path to ``binary`` (login-shell PATH), or None."""
        r = self._conn.run(_login("command -v " + shlex.quote(binary)), warn=True)
        return r.stdout.strip() or None

    def available(self, binary: str) -> bool:
        return self.locate(binary) is not None

    def run(self, argv: List) -> subprocess.CompletedProcess:
        """Run ``argv`` on the node, streaming its output; return the result."""
        r = self._conn.run(_login(_shell_argv(argv)), warn=True)
        return subprocess.CompletedProcess(list(argv), r.return_code, r.stdout, r.stderr)

    def spawn(self, argv: List) -> _Detached:
        """Launch ``argv`` fully detached on the node (new session, stdio off).

        The SSH channel closes immediately; the process keeps running on the
        node. Used for the model launch (which otherwise foreground-tails the
        model log and would hold the converge open until the model exits).
        """
        inner = "setsid nohup " + _shell_argv(argv) + " </dev/null >/dev/null 2>&1 &"
        r = self._conn.run(_login(inner), warn=True)
        return _Detached(list(argv), r.return_code)


class RemoteInstallFS:
    """File access to the node's install dir over the runtime's connection.

    Mirrors :class:`~sparklab.core.node.LocalInstallFS`: ``write`` / ``read`` /
    ``exists`` / ``hash_files`` / ``list_recipes`` / ``base_exists``.
    """

    def __init__(self, runtime: RemoteRuntime):
        self.rt = runtime
        self._base: Optional[str] = None

    @property
    def base(self) -> str:
        if self._base is None:
            self._base = self.rt.expand(self.rt.target.install_dir)
        return self._base

    def base_exists(self) -> bool:
        r = self.rt.conn.run(_login("test -d " + shlex.quote(self.base)), warn=True)
        return r.return_code == 0

    def path_str(self, rel: str) -> str:
        """The node-side absolute path for an install-relative file."""
        return f"{self.base}/{str(rel).lstrip('/')}"

    def exists(self, rel: str) -> bool:
        r = self.rt.conn.run(_login("test -f " + shlex.quote(self.path_str(rel))), warn=True)
        return r.return_code == 0

    def read(self, rel: str) -> Optional[bytes]:
        """The file's bytes, or None if it does not exist on the node."""
        try:
            with self.rt.conn.open(self.path_str(rel), "rb") as fh:
                return fh.read()
        except (IOError, OSError):
            return None

    def write(self, rel: str, data: bytes) -> str:
        dest = self.path_str(rel)
        self.rt.conn.run(_login("mkdir -p " + shlex.quote(os.path.dirname(dest))), warn=True)
        with tempfile.NamedTemporaryFile(delete=False, prefix="sparklab-") as f:
            f.write(data)
            tmp = f.name
        try:
            self.rt.conn.put(tmp, dest)
        finally:
            os.unlink(tmp)
        return dest

    def hash_files(self, rels: List[str]) -> Dict[str, Optional[str]]:
        """sha256 per rel (None where the file is absent) — read, then hash."""
        out: Dict[str, Optional[str]] = {}
        for rel in rels:
            data = self.read(rel)
            out[rel] = (None if data is None else state_mod.sha256_bytes(data))
        return out

    def list_recipes(self) -> List[str]:
        """Recipe basenames (no .yaml) under ``sparkrun/recipes`` on the node."""
        d = self.path_str("sparkrun/recipes")
        r = self.rt.conn.run(_login(f"ls {shlex.quote(d)} 2>/dev/null | sed 's/\\.yaml$//'"),
                             warn=True)
        if r.return_code != 0:
            return []
        return sorted(r.stdout.split())


class RemoteState:
    """``State`` semantics over the node's ``state.json`` (in its spark-lab
    checkout: ``<repo_dir>/.sparklab-state/state.json``).

    Same read/write model as :class:`~sparklab.core.state.State`; the file is
    the single source of truth on the managed node, so node-local and remote
    operation share one record.
    """

    STATE_REL = ".sparklab-state/state.json"

    def __init__(self, runtime: RemoteRuntime):
        self.rt = runtime

    @property
    def path(self) -> str:
        return f"{self.rt.expand(self.rt.target.repo_dir)}/{self.STATE_REL}"

    def load(self) -> dict:
        r = self.rt.conn.run(_login("cat " + shlex.quote(self.path)), warn=True)
        if r.return_code != 0 or not r.stdout.strip():
            return {"files": {}}
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"files": {}}
        return data if isinstance(data, dict) else {"files": {}}

    def save(self, data: dict) -> None:
        payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
        dest = self.path
        self.rt.conn.run(_login("mkdir -p " + shlex.quote(os.path.dirname(dest))), warn=True)
        with tempfile.NamedTemporaryFile(delete=False, prefix="sparklab-state-") as f:
            f.write(payload)
            tmp = f.name
        try:
            self.rt.conn.put(tmp, dest)
        finally:
            os.unlink(tmp)

    @property
    def files(self) -> dict:
        return self.load().get("files", {})

    @property
    def model(self):
        return self.load().get("model")

    def set_state(self, files: dict, model) -> None:
        data: Dict = {"files": files}
        if model:
            data["model"] = model
        self.save(data)

    def clear(self) -> None:
        self.rt.conn.run(_login("rm -f " + shlex.quote(self.path)), warn=True)
