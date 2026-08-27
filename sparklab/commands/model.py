"""`spark-lab model` — model workload actions (the stack keeps running).

v3 (ADR 0008) scale operations -- the config's ``models.<m>.hosts`` list is the
scale:

* ``model up <model> [--hosts a,b]`` -- add host(s) to the model's ``hosts:``
  (all hosts not yet serving it when ``--hosts`` is unset) and converge them.
  Starting is routine: the ensure path starts what isn't running.
* ``model down <model> --yes [--hosts a,b]`` -- remove host(s) from the model's
  ``hosts:`` and converge them, which stops the workloads that are no longer
  current (stale-recipe stops are gated, so this passes the gate deliberately).
* ``model stop --yes`` (legacy) -- stop the active model on the target host(s)
  WITHOUT changing the config; a routine ``apply`` starts it again. Works on
  v1/v2/v3 alike.

Up/down rewrite ``config.yaml`` on disk (YAML round-trip: comments in the
file are not preserved). Both refuse when two active models would share a host.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from ..core import cluster, config, converge, node
from ..util import run_command
from . import apply as apply_cmd


# --------------------------------------------------------------------------- #
# shared plumbing
# --------------------------------------------------------------------------- #
def _targets(args, cfg=None):
    cfg = cfg or config.load(args.config)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    return cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))


def _check_conflict(cfg, model: str, host_names):
    """Refuse when another active model would share a host with `model`."""
    data = cfg.data
    models = data.get("models") or {}
    active_others = [a for a, m in models.items()
                     if a != model and (m or {}).get("active")]
    first = str((data.get("active_models") or [None])[0] or "")
    if first and first != model and first in models and first not in active_others:
        active_others.append(first)
    all_names = [s.name for s in cfg.host_specs]
    for other in active_others:
        o_hosts = (models[other] or {}).get("hosts")
        o_set = set(all_names) if o_hosts is None else set(o_hosts)
        shared = o_set & set(host_names)
        if shared:
            raise ValueError(
                f"cannot serve '{model}' on {', '.join(sorted(shared))}: "
                f"active model '{other}' already serves it (one model per host)")


# --------------------------------------------------------------------------- #
# model up
# --------------------------------------------------------------------------- #
def up(args) -> int:
    cfg = config.load(args.config)
    if not cfg.is_v3 or cfg.view_host is not None:
        print("[ERROR] `model up` needs a v3 cluster config (a `hosts:` list).",
              file=sys.stderr)
        print("Migrate with `spark-lab migrate` (or hand-write the `hosts:` list).",
              file=sys.stderr)
        return 1
    model = args.model
    models = cfg.data.get("models") or {}
    if model not in models:
        print(f"[ERROR] no such model '{model}' (models: {', '.join(models) or '(none)'}).",
              file=sys.stderr)
        return 1

    all_names = [s.name for s in cfg.host_specs]
    current = cfg.model_host_list(model)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    targets_names = names if names else [n for n in all_names if n not in current]
    if not targets_names:
        print(f"Model '{model}' is already served on every host "
              f"({', '.join(all_names)}). Nothing to scale up.")
        return 0
    try:
        _check_conflict(cfg, model, targets_names)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    fresh = [n for n in targets_names if n not in current]
    # keep config order, append the new hosts in config order
    new_hosts = current + [n for n in all_names if n in fresh]
    if fresh:
        data = yaml.safe_load(cfg.config_path.read_text()) or {}
        entry = data.setdefault("models", {}).setdefault(model, {})
        entry["hosts"] = new_hosts
        if not entry.get("active"):
            entry["active"] = True
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        cfg.config_path.write_text(text)
        print(f"Updated {cfg.config_path}: models.{model}.hosts = "
              f"[{', '.join(new_hosts)}]\n")

    cfg2 = config.load(args.config)
    ts = cluster.targets(cfg2, targets_names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    print(f"== spark-lab model up {model} ==")
    print(f"   hosts : {', '.join(t.name for t in ts)}")
    return cluster.run_on_each(ts, lambda t: apply_cmd._converge_one(
        t, dry=False, allow_restart=False, diff=False))


# --------------------------------------------------------------------------- #
# model down
# --------------------------------------------------------------------------- #
def down(args) -> int:
    if not getattr(args, "yes", False):
        print("Refusing to scale down without --yes.", file=sys.stderr)
        print("This stops the model workload on the selected host(s) and updates", file=sys.stderr)
        print("the config so a routine `apply` leaves it stopped there. --")
        return 1
    cfg = config.load(args.config)
    if not cfg.is_v3 or cfg.view_host is not None:
        print("[ERROR] `model down` needs a v3 cluster config (a `hosts:` list).",
              file=sys.stderr)
        return 1
    model = args.model
    models = cfg.data.get("models") or {}
    if model not in models:
        print(f"[ERROR] no such model '{model}' (models: {', '.join(models) or '(none)'}).",
              file=sys.stderr)
        return 1

    all_names = [s.name for s in cfg.host_specs]
    current = cfg.model_host_list(model)
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    if names:
        bad = [n for n in names if n not in current]
        if bad:
            print(f"[ERROR] '{model}' is not served on: {', '.join(bad)} "
                  f"(currently: {', '.join(current) or '(nowhere)'}).", file=sys.stderr)
            return 1
        targets_names = list(names)
    else:
        targets_names = list(current)
    if not targets_names:
        print(f"Model '{model}' is not served on any host. Nothing to scale down.")
        return 0

    remaining = [n for n in current if n not in targets_names]
    data = yaml.safe_load(cfg.config_path.read_text()) or {}
    entry = data["models"][model]
    if remaining:
        entry["hosts"] = remaining
    else:
        entry["hosts"] = []
        entry.pop("active", None)     # fully scaled down: not active anywhere
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    cfg.config_path.write_text(text)
    print(f"Updated {cfg.config_path}: models.{model}.hosts = "
          f"[{', '.join(remaining) or '(scaled down)'}]\n")

    cfg2 = config.load(args.config)
    ts = cluster.targets(cfg2, targets_names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    print(f"== spark-lab model down {model} ==")
    print(f"   hosts : {', '.join(t.name for t in ts)}")
    return cluster.run_on_each(ts, lambda t: apply_cmd._converge_one(
        t, dry=False, allow_restart=True, diff=False))


# --------------------------------------------------------------------------- #
# model stop (legacy: stop now, config unchanged, next apply restarts)
# --------------------------------------------------------------------------- #
def _stop_one(t) -> int:
    cfg, runtime = t.cfg, t.runtime
    sparkrun = converge.find_sparkrun(runtime)
    home = runtime.home_path() if runtime is not None else None
    node_path = getattr(cfg, "node_path", None)
    if callable(node_path):
        recipe_file = node_path(f"sparkrun/recipes/{cfg.recipe_name}.yaml", home)
    else:  # pragma: no cover - test fakes
        recipe_file = str(Path(str(cfg.install_dir)) / "sparkrun" / "recipes"
                          / f"{cfg.recipe_name}.yaml")
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
    _, st = t.env()
    st.set_state(st.files, None)   # record the stop; file hashes untouched
    print("Model stopped. State updated: the next `apply` will start it again "
          "(idempotent `sparkrun run --ensure`).")
    return 0


def stop(args) -> int:
    if not getattr(args, "yes", False):
        print("Refusing to stop the model without --yes.", file=sys.stderr)
        print("This stops the model workload only; the LiteLLM + monitoring stack", file=sys.stderr)
        print("keeps running. To stop the whole stack: `spark-lab teardown --yes`. --")
        return 1
    cfg = config.load(args.config)
    ts = _targets(args, cfg)
    if not ts:
        return 1
    print(f"== spark-lab model stop ==")
    print(f"   hosts : {', '.join(t.name for t in ts)}")
    return cluster.run_on_each(ts, _stop_one)
