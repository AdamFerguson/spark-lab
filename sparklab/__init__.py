"""spark-lab engine.

One file + one command to self-host an OpenAI-compatible LLM on an NVIDIA DGX
Spark. See ``SPEC.md`` and ``docs/``.

Package layout (ADR 0001):
  - ``sparklab.cli``       -- argparse + dispatch only (no logic).
  - ``sparklab.commands``  -- thin, one module per subcommand.
  - ``sparklab.core``      -- pure engine (config, render, converge, state,
                              runtime seam). Importable without a node.
"""

__version__ = "0.1.0"
