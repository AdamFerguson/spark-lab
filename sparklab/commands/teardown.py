"""`spark-lab teardown` — stop the model + remove the stack (per selected host)."""
from __future__ import annotations

import sys

from ..core import cluster, config, converge
from ..util import run_command


def _teardown_one(t, purge: bool) -> int:
    cfg, runtime = t.cfg, t.runtime
    sparkrun = converge.find_sparkrun(runtime)
    home = runtime.home_path() if runtime is not None else None
    compose_file = cfg.node_path("litellm/docker-compose.yml", home)
    # stop by the recipe's file path (sparkrun's bare-name lookup hits its
    # registries, not our install dir) + host targeting (single-node needs --hosts).
    # Tolerant of "no running workload" (teardown must still remove the stack
    # when nothing is running -- see converge.tolerant_stop_argv).
    recipe_file = cfg.node_path(f"sparkrun/recipes/{cfg.recipe_name}.yaml", home)
    stop_argv = converge.tolerant_stop_argv(
        sparkrun, recipe_file,
        ["--cluster", cfg.cluster_name] if cfg.is_cluster
        else ["--hosts", ",".join(str(h) for h in cfg.hosts)])
    print("Stopping model workload...")
    run_command(stop_argv, ok=True, runtime=runtime)
    down_argv = ["docker", "compose", "-f", compose_file, "down"]
    if purge:
        down_argv.append("-v")
    print("Tearing down the LiteLLM + monitoring stack...")
    run_command(down_argv, ok=True, runtime=runtime)
    # Clear the recorded state so the next `apply` re-converges from scratch
    # (a teardown leaves the node with no running managed services). The state
    # file lives on the managed node, so this clears it there.
    _, st = t.env()
    st.clear()
    print("Done. State cleared (re-run `apply` to bring it back up).")
    return 0


def run(args) -> int:
    cfg = config.load(args.config)
    if not args.yes:
        print("Refusing to tear down without --yes.", file=sys.stderr)
        print("This stops the model workload and removes the LiteLLM containers", file=sys.stderr)
        print("on every selected host. Named volumes are KEPT -- the LiteLLM", file=sys.stderr)
        print("database (litellm_postgres_data), redis (litellm_redis_data) and the", file=sys.stderr)
        print("observability data all survive; a re-apply restores the stack on the", file=sys.stderr)
        print("same data. Add --purge to destroy the named volumes too. --", file=sys.stderr)
        return 1
    if args.purge:
        print("WARNING: --purge DESTROYS the named volumes on every selected host:",
              file=sys.stderr)
        print("  litellm_postgres_data   -- the LiteLLM/Postgres database (model list,",
              file=sys.stderr)
        print("                            spend/auth data): UNRECOVERABLE", file=sys.stderr)
        print("  litellm_redis_data      -- gateway cache/queues", file=sys.stderr)
        print("  litellm_prometheus_data / litellm_grafana_data -- observability state",
              file=sys.stderr)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    return cluster.run_on_each(ts, lambda t: _teardown_one(t, args.purge))
