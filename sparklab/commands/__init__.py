"""sparklab.commands — thin, one module per subcommand.

Each module exposes ``run(args) -> int``. ``sparklab.cli`` builds the argparse
tree and dispatches to these. Business logic lives in ``sparklab.core``; these
modules are thin orchestration + presentation (they obtain the runtime seam
from ``args.runtime`` and call into ``core`` / :mod:`sparklab.util`).
"""
