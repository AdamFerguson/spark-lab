"""`spark-lab zoo prepare` -- install + start the llama-swap service on zoo hosts.

Idempotent. Per swap host (a host with swap-enabled models, ADR-0010):
converges the zoo files (node-side recipes + llama-swap config/unit),
verifies the llama-swap BINARY is installed (downloading it stays a
deliberate, documented step -- see OPERATIONS), then installs the rendered
systemd USER unit and enables it (with lingering, so the zoo sits ready
across reboots). Safe to re-run any time.
"""

from __future__ import annotations

import shlex
import sys
import tempfile
from pathlib import Path

from ..core import cluster, config as config_mod, converge, render


def _prepare_one(t, linger: bool) -> int:
    cfg, runtime = t.cfg, t.runtime
    fs, _st = t.env()
    if not fs.base_exists():
        print(f"[ERROR] install dir not found on {t.name} -- apply first", file=sys.stderr)
        return 1
    rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-zoo-")))
    converge.write_files(cfg, rendered, dry_run=False, fs=fs)
    print(f"   converged zoo files on {t.name} ({len(rendered)} file(s) ensured)")

    home = runtime.home_path() if runtime is not None else None
    bin_raw = str(cfg.swap().get("bin") or (cfg.install_dir_raw.rstrip("/") + "/bin/llama-swap"))
    # `~` must expand: quote-protection would suppress it. Map to the node
    # user's home explicitly (remote home when known, $HOME otherwise).
    h = home or "$HOME"
    tilde_home = h + bin_raw[1:] if bin_raw.startswith("~") else (h if bin_raw == "~" else bin_raw)
    rc = runtime.run(["sh", "-lc", f'test -x "{tilde_home}"']).returncode
    if rc != 0:
        print(f"[ERROR] llama-swap binary not found on {t.name}: {bin_raw}", file=sys.stderr)
        print("        download the release binary there once (see OPERATIONS 'model zoo'),", file=sys.stderr)
        print("        then re-run `spark-lab zoo prepare`.", file=sys.stderr)
        return 1

    unit_node = converge._install_rel_path(cfg, "llama-swap/llama-swap.service", home)
    install = converge.user_systemd_argv(
        "mkdir -p ~/.config/systemd/user"
        f" && install -m 644 {shlex.quote(unit_node)} ~/.config/systemd/user/llama-swap.service"
        " && systemctl --user daemon-reload"
        " && systemctl --user enable --now llama-swap"
    )
    if runtime.run(install).returncode != 0:
        print(f"[ERROR] failed to install/start llama-swap.service --user on {t.name}", file=sys.stderr)
        return 1

    if linger:
        try:
            r = runtime.run_sudo(["loginctl", "enable-linger"])
            linger_ok = r.returncode == 0
        except Exception:
            linger_ok = False
        if not linger_ok:
            print(
                "   note: lingering not enabled (needs sudo from a terminal). Without it "
                "the user manager can stop at logout/boot -- run `sudo loginctl "
                "enable-linger` on the node.",
                file=sys.stderr,
            )

    port = cfg.swap_port()
    ok = runtime.run(
        [
            "sh",
            "-c",
            f"for i in $(seq 1 15); do curl -fsS -o /dev/null "
            f"http://127.0.0.1:{port}/health && exit 0; sleep 2; done; exit 1",
        ]
    ).returncode
    if ok != 0:
        print(
            f"[ERROR] llama-swap is not answering on {t.name}:{port} -- check "
            f"`journalctl --user -u llama-swap` on the node",
            file=sys.stderr,
        )
        return 1
    print(f"   llama-swap ready on {t.name}:{port} (zoo models: {', '.join(cfg.swap_aliases())})")
    return 0


def run(args) -> int:
    cfg = config_mod.load(args.config)
    if not cfg.swap_enabled():
        print(
            "swap.enabled is false -- nothing to prepare (add swap.enabled + swap-enabled models to config.yaml).",
            file=sys.stderr,
        )
        return 1
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = [t for t in cluster.targets(cfg, names, runtime=getattr(args, "runtime", None)) if t.cfg.swap_for_host()]
    if not ts:
        print("no swap-enabled models placed on the selected hosts", file=sys.stderr)
        return 1
    print("== spark-lab zoo prepare ==")
    print(f"   zoo hosts: {', '.join(t.name for t in ts)}")
    rc = cluster.run_on_each(ts, lambda t: _prepare_one(t, linger=True))
    if rc == 0:
        print(
            "\nZoo is live: request any zoo model through the gateway and llama-swap "
            "loads it (idle models unload after their ttl)."
        )
    return rc
