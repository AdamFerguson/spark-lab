"""`spark-lab status` — show workloads, stack, network status."""
from __future__ import annotations

from ..core import config, converge
from ..util import run_command


def run(args) -> int:
    cfg = config.load(args.config)
    runtime = getattr(args, "runtime", None)
    sparkrun = converge.find_sparkrun(runtime)
    home = runtime.home_path() if runtime is not None else None
    compose_file = cfg.node_path("litellm/docker-compose.yml", home)
    if cfg.is_remote:
        print(f"== status on {runtime.label} (remote) ==")
    print("== sparkrun ==")
    run_command([sparkrun, "status"], ok=True, runtime=runtime)
    print("\n== docker compose ==")
    run_command(["docker", "compose", "-f", compose_file, "ps"], ok=True, runtime=runtime)
    print("\n== tailscale ==")
    run_command(["tailscale", "status"], ok=True, runtime=runtime)
    if cfg.cloudflare().get("enabled", False):
        print("\n== cloudflare ==")
        run_command(["systemctl", "is-active", "cloudflared"], ok=True, runtime=runtime)
    return 0
