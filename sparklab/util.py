"""Cross-cutting helpers shared by commands.

The single place that turns an ``argv`` list into a subprocess call, routed
through the runtime seam (ADR 0002). Commands import :func:`run_command` so they
never touch ``subprocess`` directly.
"""
from __future__ import annotations

from .core import runtime as runtime_mod


def run_command(argv, ok: bool = False, runtime=None) -> int:
    """Run an operational command, streaming its output; return its exit code.

    Routed through the runtime seam (ADR 0002). ``ok`` is accepted for signature
    stability (historical call sites pass it) but is a no-op. If the binary is
    not on PATH, the call is skipped and ``0`` is returned (read-only / optional
    commands must not fail the run just because a tool is absent).
    """
    if runtime is None:
        runtime = runtime_mod.default_runtime()
    if not runtime.available(str(argv[0])):
        print(f"(skipping: '{argv[0]}' not found on PATH)")
        return 0
    return runtime.run(list(argv)).returncode
