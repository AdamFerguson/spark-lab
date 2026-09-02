"""`spark-lab sync` — the PULL half of the loop (reality -> config).

Reads every selected node's live state (inventory.discover + on-disk file
hashes) and reports it against `config.yaml`:

  * engines that run but are NOT exposed by the gateway  (`--write` adds
    litellm.extra_models entries for them and converges the gateways)
  * models the config expects to be served but the gateway does not serve
  * gateway entries served from nowhere (ghosts to clean up by hand)
  * file drift between the nodes and what the config renders; `--write`
    refreshes each node's state file from on-disk reality (what `adopt`
    does, per host, in one pass)

`--write` never touches model workloads and never overwrites node files:
config gets exposure entries added (YAML round-trip: comments not
preserved), node state adopts reality, drift reports are advisory.
"""

from __future__ import annotations

import sys

from ..core import cluster, config as config_mod, inventory, render, state
from . import adopt as adopt_cmd, apply as apply_cmd, expose as expose_cmd
from pathlib import Path


def _file_drift(t) -> list:
    """Files on the node that differ from what this host's view renders."""
    fs, _ = t.env()
    if not fs.base_exists():
        return []
    import tempfile

    rendered = render.render(t.cfg, Path(tempfile.mkdtemp(prefix="sparklab-sync-")))
    drift = []
    for rel, data in rendered.items():
        on_disk = fs.read(rel)
        if on_disk is not None and state.sha256_bytes(on_disk) != state.sha256_bytes(data):
            drift.append(rel)
    return drift


def run(args) -> int:
    cfg = config_mod.load(args.config)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    write = bool(getattr(args, "write", False))
    print(f"== spark-lab sync {'[--write]' if write else '(read-only report)'} ==")
    print(f"   hosts : {', '.join(t.name for t in ts)}")

    invs = {t.name: inventory.discover(t.runtime, t.cfg) for t in ts}
    served = set()
    for inv in invs.values():
        if inv["gateway"] and inv["gateway"]["reachable"]:
            served.update(inv["gateway"]["served"])

    expected = {r[2] for r in cfg.placement_table() if r[2]}  # gateway names from placement
    declared = {m.get("model_name") for m in cfg.litellm.get("extra_models") or []}

    print("\n-- live engines --")
    unexposed = []
    for host, inv in invs.items():
        for e in inv["engines"]:
            if not e["models"]:
                continue
            hit = e["models"][0] in served
            mark = "exposed" if hit else "NOT EXPOSED"
            print(f"   {host}/{e['container']:<26} :{e['port']:<6} {e['models'][0]:<36} [{mark}]")
            if not hit:
                unexposed.append((host, e))
    if not any(i["engines"] for i in invs.values()):
        print("   (no engines answering on any host)")

    missing = sorted(expected - served)
    if missing:
        print("\n-- config expects, gateway does not serve --")
        for m in missing:
            print(f"   {m}   (engine down? or still loading)")
    ghosts = sorted(served - expected - declared)
    if ghosts:
        print("\n-- gateway serves, config knows nothing about (ghosts) --")
        for g in ghosts:
            print(f"   {g}   (remove from litellm.extra_models or start its engine)")

    print("\n-- file drift (node vs render) --")
    drifted_hosts = []
    for t in ts:
        d = _file_drift(t)
        if d:
            drifted_hosts.append(t)
            print(f"   {t.name}: {len(d)} file(s) drift")
            for rel in d[:8]:
                print(f"      - {rel}")
        else:
            print(f"   {t.name}: none")

    if not write:
        n_un = len(unexposed)
        print(
            "\nRead-only. `sync --write` adds extra_models entries for the "
            f"{n_un} unexposed engine(s) and refreshes node state"
            " (never touches model workloads)."
        )
        return 0

    if unexposed:
        cp = Path(cfg.config_path)
        for host, e in unexposed:
            spec = cfg.select_hosts([host])[0]
            entry = expose_cmd.entry_for(e["models"][0], spec.ip or spec.ssh_host or spec.name, e["port"])
            try:
                expose_cmd.add_extra_model(cp, entry)
            except ValueError as err:
                print(f"[WARN] {err}", file=sys.stderr)
                continue
            print(f"   + extra_models: {entry['model_name']} -> {entry['litellm_params']['api_base']}")
        print("   (config YAML round-tripped: comments not preserved)")
        # Gateway files matter to EVERY control-plane host -- converge all of
        # them, not just the hosts this sync was scoped to (a scoped --write
        # that skipped a gateway would leave it serving the old list).
        fresh = config_mod.load(args.config)
        gw_names = [s.name for s in fresh.host_specs if fresh.view_for(s.name).control_plane_enabled()]
        if gw_names:
            rc = apply_cmd.run(_apply_ns(args, ",".join(gw_names)))
        else:
            print("   (no control-plane hosts: nothing to converge)")
            rc = 0
    else:
        rc = 0
    for t in ts:
        adopt_cmd._adopt_one(t, dry=False)  # refresh node state from on-disk reality
    return rc


def _apply_ns(args, hosts=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        config=args.config,
        hosts=hosts,
        dry_run=False,
        no_model=True,
        restart_model=False,
        diff=False,
        verbose=False,
        json=False,
        runtime=args.runtime,
    )
