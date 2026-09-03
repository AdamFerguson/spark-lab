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


def _kit_abs(cfg, runtime, kit: str) -> str:
    """Absolute (quote-ready) node path for a kit dir.

    `~` may lead EITHER the kit path or the install dir (~/AI) -- quoted, it
    would never expand, so map it to the node home explicitly ($HOME when the
    runtime cannot answer, which login shells expand)."""
    full = kit if kit.startswith(("/", "~")) else f"{cfg.install_dir_raw.rstrip('/')}/{kit.lstrip('/')}"
    if full.startswith("~"):
        home = runtime.home_path() if runtime is not None else None
        full = (home or "$HOME") + full[1:]
    return shlex.quote(full)


def _prepare_one(t, linger: bool) -> int:
    cfg, runtime = t.cfg, t.runtime
    fs, _st = t.env()
    if not fs.base_exists():
        print(f"[ERROR] install dir not found on {t.name} -- apply first", file=sys.stderr)
        return 1
    rendered = render.render(cfg, Path(tempfile.mkdtemp(prefix="sparklab-zoo-")))
    converge.write_files(cfg, rendered, dry_run=False, fs=fs)
    print(f"   converged zoo files on {t.name} ({len(rendered)} file(s) ensured)")

    # Script-mode (kit) preflight: fail HERE, not at swap time (ADR-0010 add.).
    for alias in cfg.swap_aliases():
        mdef = (cfg.data.get("models") or {}).get(alias) or {}
        sc = cfg.swap_script(mdef)
        if not sc:
            continue
        stop = str(sc.get("stop", "stop.sh"))
        kq = _kit_abs(cfg, runtime, str(sc["kit"]))
        chk = runtime.run(
            [
                "sh",
                "-lc",
                f"test -d {kq} && test -x {kq}/start.sh && test -x {kq}/{shlex.quote(stop)} && test -f {kq}/.env",
            ]
        )
        if chk.returncode != 0:
            print(
                f"[ERROR] kit for swap model '{alias}' is not usable on {t.name}: {str(sc['kit'])} "
                f"-- need kit dir + executable start.sh + executable {stop} + .env "
                "(clone the kit onto this node and configure its .env)",
                file=sys.stderr,
            )
            return 1
        print(f"   kit ok for '{alias}'")

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


def _import_kit(t, kit: str) -> int:
    """`zoo import`: read a kit's .env/scripts, print a paste-ready swap block
    (no silent config rewriting -- the owner pastes what they want)."""
    cfg, runtime = t.cfg, t.runtime
    env_txt = runtime.run_capture(["sh", "-lc", f"cat {_kit_abs(cfg, runtime, kit)}/.env 2>/dev/null"]).stdout or ""
    if "=" not in env_txt:
        print(f"[ERROR] no readable .env in kit {kit} on {t.name} (clone it / copy .env.sample)", file=sys.stderr)
        return 1
    kv = {}
    for line in env_txt.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        kv[k.strip()] = v.strip().strip('"').strip("'")
    kitdir = _kit_abs(cfg, runtime, kit)
    grep = (
        runtime.run_capture(
            ["sh", "-lc", f"grep -rhoE 'CONTAINER_NAME=.{{0,40}}' {kitdir} --include='*.sh' 2>/dev/null | head -1"]
        ).stdout
        or ""
    )
    cont = grep.strip().split("=", 1)[-1].strip('"').strip("'") or "<container>"
    port = kv.get("PORT", "<port>")
    served = kv.get("SERVED_MODEL_NAME") or kv.get("MODEL_ID") or "<served-id>"
    spanning = "WORKER_IP" in kv and bool(str(kv.get("WORKER_IP", "")).strip())
    alias = Path(kit.rstrip("/")).name.lower().replace(" ", "-")
    host_note = "   # kit head: the zoo daemon runs where start.sh runs" if spanning else ""
    print(
        f"  {alias}:\n"
        f"    active: false\n"
        f"    hosts: [{t.name}]{host_note}\n"
        f"    swap:\n"
        f"      enabled: true\n"
        f"      pinned: true          # kit cold starts are long -- explicit `swap unload` only\n"
        f"      port: {port}          # kit .env PORT\n"
        f"      script:\n"
        f"        kit: {kit}\n"
        f"        container: {cont}   # kit CONTAINER_NAME (docker-wait target)\n"
        f'        start_args: ["--no-download"]\n'
        f"        served: {served}\n"
    )
    print(f"# paste under `models:` in config.yaml, then: apply --hosts {t.name} && zoo prepare --hosts {t.name}")
    return 0


def run(args) -> int:
    cfg = config_mod.load(args.config)
    if getattr(args, "zoo_cmd", "prepare") == "import":
        host = getattr(args, "host", None) or cfg.host_specs[0].name
        tt = next((x for x in cluster.targets(cfg, [host], runtime=getattr(args, "runtime", None))), None)
        if tt is None:
            print(f"unknown host '{host}'", file=sys.stderr)
            return 1
        return _import_kit(tt, args.kit)
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
