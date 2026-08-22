"""sparklab.core — the pure, side-effect-free engine.

Everything here is importable without docker/sparkrun/a node:
config + schema, Jinja rendering, converge planning, state, and the runtime
seam (ADR 0002). Commands (``sparklab.commands``) orchestrate these; the
runtime seam is the only place node side-effects happen.
"""
