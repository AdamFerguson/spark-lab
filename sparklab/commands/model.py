"""`spark-lab model stop` — stop the model workload only.

The LiteLLM + monitoring stack keeps running; this just stops the model
container(s) for the active recipe (the targeted counterpart of
`spark-lab teardown`, which takes the whole stack down).

The stop is recorded in state (the model entry is cleared), so the node's
record matches reality: the next routine `apply` re-starts the model
(`sparkrun run --ensure` starts what isn't running) and records it running
again. Stop is gated behind `--yes` like teardown.
"""
from __future__ import annotations

import sys

from ..core import config, converge, node
from ..util import run_command


def _recipe_path(cfg, home):
    """The node-side path of the active recipe (same resolution as teardown)."""
    node_path = getattr(cfg, "node_path", None)
    if callable(node_path):
        return node_path(f"sparkrun/recipes/{cfg.recipe_name}.yaml", home)
    from pathlib import Path
    return str(Path(str(cfg.install_dir)) / "sparkrun" / "recipes" / f"{cfg.recipe_name}.yaml")


def run(args) -> int:
    cfg = config.load(args.config)
    if not args.yes:
        print("Refusing to stop the model without --yes.", file=sys.stderr)
        print("This stops the model workload only; the LiteLLM + monitoring stack", file=sys.stderr)
        print("keeps running. To stop the whole stack: `spark-lab teardown --yes`. --")
        return 1

    runtime = getattr(args, "runtime", None)
    if cfg.is_remote:
        print(f"== spark-lab model stop on {runtime.label} (remote) ==")

    sparkrun = converge.find_sparkrun(runtime)
    home = runtime.home_path() if runtime is not None else None
    recipe_file = _recipe_path(cfg, home)
    stop_argv = [sparkrun, "stop", recipe_file]
    if cfg.is_cluster:
        stop_argv += ["--cluster", cfg.cluster_name]
    else:
        stop_argv += ["--hosts", ",".join(str(h) for h in cfg.hosts)]

    print(f"Stopping model workload '{cfg.recipe_name}' (stack stays up)...")
    rc = run_command(stop_argv, ok=True, runtime=runtime)
    if rc != 0:
        print(f"(sparkrun stop returned {rc} — was the model running?)", file=sys.stderr)
        return rc

    # Record the stop: the model is no longer confirmed running. File hashes are
    # kept untouched — only the model entry changes.
    _, st = node.node_env(cfg, runtime)
    st.set_state(st.files, None)
    print("Model stopped. State updated: the next `apply` will start it again "
          "(idempotent `sparkrun run --ensure`).")
    return 0
