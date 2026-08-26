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


class Runtime:
    """Real runtime: executes commands on the node via ``subprocess``."""

    def available(self, binary: str) -> bool:
        """True if ``binary`` can be found on PATH."""
        return shutil.which(binary) is not None

    def run(self, argv) -> subprocess.CompletedProcess:
        """Run ``argv`` (streaming output) and return the ``CompletedProcess``.

        Callers that want read-only "skip if the binary is missing" behavior do
        that check themselves (see ``sparklab.cli._run``); this is just the seam.
        """
        return subprocess.run(list(argv))

    def spawn(self, argv) -> subprocess.Popen:
        """Launch ``argv`` fully **detached** and return the ``Popen`` without
        waiting for it to exit.

        Used for the model launch: ``sparkrun run`` foreground-tails the model
        log and would otherwise block the converge forever (so the control plane
        would never start). We launch it in a new session with stdio redirected
        away so it doesn't hold the caller's terminal; the model's own logs stay
        available via ``docker logs`` / ``spark-lab logs``, and a separate bounded
        probe confirms it actually came up.
        """
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def default_runtime() -> Runtime:
    """The real runtime used outside of tests."""
    return Runtime()
