"""The command<->runtime boundary (ADR 0002).

This is the ONLY module that talks to the node (subprocess). Commands obtain a
Runtime (built once in the CLI layer, see ``sparklab.cli.main``) and route their
side-effecting operations through it instead of calling ``subprocess`` directly.

Why this exists:
  * **Testability.** Tests inject a *fake* runtime that records the commands it
    would have run (and can report canned exit codes), so the apply / status /
    teardown / upgrade paths can be exercised end-to-end with no node, no
    docker, and no sparkrun present.
  * **Single seam.** All shell-out lives in one place, so the "no node, no
    real side effects" guarantee is enforced by construction rather than by
    remembering not to call ``subprocess`` elsewhere.

The default :class:`Runtime` is behaviorally identical to the pre-seam code:
``run`` streams a command's output and returns a ``CompletedProcess``;
``available`` reports whether a binary is on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # annotation-only: remote pulls in fabric, never import eagerly
    from .remote import RemoteRuntime


class Runtime:
    """Real runtime: executes commands on the node via ``subprocess``."""

    is_remote = False

    def available(self, binary: str) -> bool:
        """True if ``binary`` can be found on PATH."""
        return shutil.which(binary) is not None

    def locate(self, binary: str):
        """The absolute path to ``binary`` on this machine, or None."""
        return shutil.which(binary)

    def home_path(self):
        """The target node's $HOME. Local runtime: the node is this machine,
        so no remote resolution is needed (None)."""
        return None

    def run(self, argv) -> subprocess.CompletedProcess:
        """Run ``argv`` (streaming output) and return the ``CompletedProcess``.

        Callers that want read-only "skip if the binary is missing" behavior do
        that check themselves (see ``sparklab.cli._run``); this is just the seam.
        """
        return subprocess.run(list(argv))

    def run_capture(self, argv) -> subprocess.CompletedProcess:
        """Run ``argv`` with stdout/stderr CAPTURED (inventory-style reads:
        the caller parses the output, nothing is streamed)."""
        return subprocess.run(list(argv), capture_output=True, text=True)

    def run_sudo(self, argv) -> subprocess.CompletedProcess:
        """Run ``argv`` under ``sudo`` on this machine.

        ``subprocess`` inherits the controlling terminal, so sudo's password
        prompt appears here and the password is typed on this machine (never
        passed through the command line)."""
        return subprocess.run(["sudo"] + list(argv))

    def spawn(self, argv, log: Optional[str] = None) -> subprocess.Popen:
        """Launch ``argv`` fully **detached** and return the ``Popen`` without
        waiting for it to exit.

        Used for the model launch: ``sparkrun run`` foreground-tails the model
        log and would otherwise block the converge forever (so the control plane
        would never start). We launch it in a new session with stdio redirected
        away so it doesn't hold the caller's terminal; the model's own logs stay
        available via ``docker logs`` / ``spark-lab logs``, and a separate bounded
        probe confirms it actually came up.

        ``log`` (a node-local path) captures the launch's own stdout/stderr
        instead of discarding it, so a failed launch can be tailed by the
        readiness probe / operator.
        """
        out = open(log, "ab") if log else subprocess.DEVNULL
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )


def default_runtime() -> Runtime:
    """The real runtime used outside of tests."""
    return Runtime()


def runtime_for(cfg) -> "Runtime | RemoteRuntime":
    """The runtime matching a config: local, or remote if ``install.remote.host``
    is set.

    The remote runtime (and therefore the fabric/paramiko dependency) is only
    constructed -- and only imported -- when the config actually targets a
    remote node, so local mode keeps its zero-SSH footprint.
    """
    if getattr(cfg, "is_remote", False):
        from .remote import RemoteRuntime, RemoteTarget

        return RemoteRuntime(RemoteTarget.from_config(cfg))
    return Runtime()
