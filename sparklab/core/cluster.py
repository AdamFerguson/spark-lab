"""Multi-host fan-out (ADR 0008): which hosts a command targets, and how to run
a per-host operation across them.

A v3 config names every managed node (``hosts:``). For a selected subset
(``--hosts`` or all), :func:`targets` builds one :class:`HostTarget` per node:
the host's config *view* (its overrides applied) + its runtime (local, or
remote over Fabric when the node isn't this machine) + its install-dir FS and
state seam. Commands loop over the targets with :func:`run_on_each` and keep
their per-host bodies unchanged.

**Local auto-detection (operator case 3):** the same config file works on the
laptop *and* on every Spark. When spark-lab runs on a node that appears in
``hosts:``, that node is converged **locally** even if its entry says
``remote: true`` (we're already here -- no SSH needed); the remaining hosts are
still reached over SSH.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Callable, List, Optional

from . import node, runtime as runtime_mod
from .config import HostSpec


class HostTarget:
    """One selected host: its config view, its runtime, its file+state seams."""

    def __init__(self, spec: HostSpec, cfg, runtime):
        self.spec = spec
        self.cfg = cfg          # the per-host view (v3) or the config itself (v1/v2)
        self.runtime = runtime
        self.name = spec.name
        self._fs = None
        self._state = None

    @property
    def is_remote(self) -> bool:
        return bool(getattr(self.runtime, "is_remote", False))

    @property
    def label(self) -> str:
        """The section-header label: 'local' or 'user@host (remote)'."""
        if self.is_remote and hasattr(self.runtime, "label"):
            return f"{self.runtime.label} (remote)"
        return "local"

    def env(self):
        """This host's ``(install_fs, state)`` (local paths or remote SFTP/SSH)."""
        if self._fs is None:
            self._fs, self._state = node.node_env(self.cfg, self.runtime)
        return self._fs, self._state


def local_identities() -> set:
    """Names that identify this machine: hostname, /etc/hostname, FQDN aliases,
    and the primary non-loopback IP."""
    ids = set()
    try:
        ids.add(socket.gethostname())
    except OSError:
        pass
    try:
        p = Path("/etc/hostname")
        if p.is_file():
            ids.add(p.read_text().strip())
    except OSError:
        pass
    try:
        infos = socket.gethostbyname_ex(socket.gethostname())
        ids.add(infos[0])
        ids.update(a for a in infos[2] if a)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))   # UDP: no packet is actually sent
        ids.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return {i for i in ids if i}


def is_on_host(spec: HostSpec) -> bool:
    """True when this machine IS ``spec``'s node (run locally, no SSH).

    ``remote: false`` entries are unconditionally local. Otherwise match this
    machine's identities against the entry's name and ssh host (full + first
    label: ``luna.tailnet.example`` matches a box whose hostname is
    ``luna``).
    """
    if not spec.remote:
        return True
    ids = local_identities()
    cands = set()
    if spec.name:
        cands.add(spec.name)
    h = spec.ssh_host
    if h:
        cands.add(h)
        cands.add(h.split(".")[0])
    return bool(cands & ids)


def build_remote(spec: HostSpec, view_cfg):
    """The Fabric-backed runtime for a remote host (one connection per run)."""
    from .remote import RemoteRuntime, RemoteTarget
    ssh = str(spec.ssh or "")
    user = spec.user or (ssh.split("@", 1)[0] if "@" in ssh else None) or os.environ.get("USER")
    target = RemoteTarget(
        host=spec.ssh_host,
        user=user,
        port=spec.port,
        identity_file=spec.identity_file,
        install_dir=str(view_cfg.install_dir_raw),
        repo_dir=str(view_cfg.repo_dir),
    )
    return RemoteRuntime(target)


def targets(cfg, names: Optional[List[str]] = None,
            runtime: Optional[object] = None,
            remote_factory: Optional[Callable] = None) -> List[HostTarget]:
    """Build a :class:`HostTarget` per selected host of ``cfg``.

    ``runtime``: an injected runtime used for LOCAL targets (tests inject a
    recording fake). ``remote_factory``: builds the remote runtime for a
    non-local host (tests inject a stub connection); defaults to
    :func:`build_remote`.
    """
    specs = cfg.select_hosts(names)
    out: List[HostTarget] = []
    local_rt = None
    for spec in specs:
        view = cfg.view_for(spec.name)
        if is_on_host(spec):
            if local_rt is None:
                local_rt = runtime if runtime is not None else runtime_mod.default_runtime()
            rt = local_rt
        elif not cfg.is_v3 and runtime is not None and getattr(runtime, "is_remote", False):
            # legacy v1/v2 remote config: the CLI already built this config's
            # remote runtime; reuse it (byte-identical to the old single-runtime
            # behavior).
            rt = runtime
        else:
            rt = (remote_factory or build_remote)(spec, view)
        out.append(HostTarget(spec, view, rt))
    return out


def parse_hosts_arg(value: Optional[str]) -> Optional[List[str]]:
    """The ``--hosts`` CLI value -> a host-name list (None when unset/empty)."""
    if not value:
        return None
    names = [p.strip() for p in value.split(",") if p.strip()]
    return names or None


def run_on_each(targets: List[HostTarget], op: Callable[[HostTarget], int]) -> int:
    """Run ``op`` per host with sectioned output; continue past a failure.

    Prints ``==> [name] label`` before each host and (for multi-host runs) a
    summary. Returns 0 only when every host succeeded.
    """
    results: List[tuple] = []
    for t in targets:
        print(f"\n==> [{t.name}] {t.label}")
        rc = op(t) or 0
        results.append((t.name, int(rc)))
    if len(targets) > 1:
        print("\n== summary ==")
        for name, rc in results:
            print(f"  {name:<14}{'ok' if rc == 0 else 'FAILED (rc ' + str(rc) + ')'}")
        bad = [n for n, rc in results if rc != 0]
        if bad:
            print(f"  {len(bad)} of {len(targets)} host(s) failed.")
    return 0 if all(rc == 0 for _, rc in results) else 1
