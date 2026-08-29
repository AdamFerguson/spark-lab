"""Recipe reference resolution (v3 placement-only model blocks).

A v3 config may declare a model *by reference* instead of inline:

    models:
      my-model:
        active: true
        hosts: [luna]
        recipe: qwen38-27b      # -> <config_dir>/recipes/qwen38-27b.yaml

The referenced file is a plain, directly-runnable **sparkrun** recipe: only
sparkrun-known top-level keys, so a user can ``sparkrun run`` it without
spark-lab (ADR-0009). spark-lab reads its two extensions from sparkrun's
documented free-form ``metadata:`` section:

* ``metadata.litellm:``           -- gateway metadata (model_name, model_info, ...)
* ``metadata.readiness_seconds:`` -- bounded apply-time ``/health`` probe bound

:func:`resolve_all` folds each referenced recipe's launch spec into the model
block (inline placement keys always win) so every downstream consumer --
render, converge, ``serving_entries`` -- sees one resolved dict and never
branches on the reference form.

Cluster-shaped values stay in the config on purpose:

* ``hosts:`` / ``active:``       -- placement (the scheduler pin source)
* ``hf_token_env:``             -- which .env var carries the HF token; the
  secret is injected into the RENDERED recipe only (repo recipes stay clean
  and secret-free; direct sparkrun users authenticate host-side via ``hf auth``)
* ``host_overrides:``           -- per-host tailoring (escape hatch)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Top-level keys a sparkrun recipe may carry -- mirrored from
# sparkrun.core.recipe._KNOWN_KEYS (sparkrun 0.3.4, 2026-08-28). Keep in sync;
# the test suite asserts every committed recipes/*.yaml against this set.
SPARKRUN_KNOWN_KEYS: frozenset = frozenset({
    "sparkrun_version", "recipe_version", "name", "description", "model",
    "model_revision", "runtime", "runtime_version", "mode", "min_nodes",
    "max_nodes", "container", "defaults", "env", "command", "runtime_config",
    "cluster_only", "solo_only", "benchmark", "metadata", "pre_exec",
    "post_exec", "post_commands", "mods", "stop_after_post", "builder",
    "builder_config", "executor", "executor_config", "scheduler",
    "distribution_config", "layout", "cluster_config",
})

# Placement keys a v3 model block owns; everything else in the block is
# inline model data that beats the referenced recipe on merge.
PLACEMENT_KEYS = ("active", "hosts", "recipe", "hf_token_env", "host_overrides")


class RecipeError(ValueError):
    """A recipe reference could not be resolved or validated."""


def recipe_dir(config_dir: Path) -> Path:
    return Path(config_dir) / "recipes"


def load_recipe_file(config_dir: Path, name: str) -> Dict[str, Any]:
    """Read + validate ``<config_dir>/recipes/<name>.yaml``.

    The file must stay a plain sparkrun recipe: only ``SPARKRUN_KNOWN_KEYS``
    top-level keys, a ``name:`` equal to the file stem, a ``model:`` and a
    launch (``command:`` or legacy ``defaults:``-based synthesis).
    """
    path = recipe_dir(config_dir) / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in recipe_dir(config_dir).glob("*.yaml")) \
            if recipe_dir(config_dir).is_dir() else []
        raise RecipeError(
            f"recipe file not found: {path}"
            + (f" (available: {', '.join(available)})" if available else ""))
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise RecipeError(f"recipe file {path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise RecipeError(f"recipe file {path} must be a YAML mapping")
    bad = sorted(k for k in data if k not in SPARKRUN_KNOWN_KEYS)
    if bad:
        raise RecipeError(
            f"recipe '{name}' carries unknown top-level key(s) {bad}: a recipe "
            "must stay a plain, directly-runnable sparkrun recipe (sparkrun "
            "would silently sweep them into runtime_config). Put gateway or "
            "probe metadata under 'metadata:' instead.")
    inner = str(data.get("name") or "")
    if inner and inner != name:
        raise RecipeError(
            f"recipe '{name}': inner 'name' is '{inner}' but the file stem is "
            f"'{name}' -- they must match (the reference is by file stem).")
    if not str(data.get("model") or "").strip():
        raise RecipeError(
            f"recipe '{name}': 'model:' (the HF model id) is required.")
    command = data.get("command")
    if not (isinstance(command, str) and command.strip()):
        has_defaults = isinstance(data.get("defaults"), dict) and data.get("defaults")
        if not has_defaults:
            raise RecipeError(
                f"recipe '{name}': needs a 'command:' (or at least 'defaults:') "
                "to launch.")
    return data


def resolve_model_block(mdef: Dict[str, Any], config_dir: Path) -> Dict[str, Any]:
    """Fold a referenced recipe into the model block (inline keys win).

    A block without ``recipe:`` passes through unchanged (legacy inline form,
    v1/v2, or a v3 block that has not migrated yet).
    """
    mdef = mdef or {}
    ref = str(mdef.get("recipe") or "").strip()
    if not ref:
        return mdef
    recipe = load_recipe_file(config_dir, ref)

    out: Dict[str, Any] = {}
    field_map = {
        "model": "hf_model",
        "container": "image",
        "runtime": "runtime",
        "min_nodes": "min_nodes",
        "model_revision": "model_revision",
        "executor_config": "executor_config",
        "env": "env",
        "command": "serve_command",
    }
    for src, dst in field_map.items():
        if src in recipe:
            out[dst] = recipe[src]
    defaults = recipe.get("defaults") or {}
    if isinstance(defaults, dict):
        out["params"] = {k: v for k, v in defaults.items() if k not in ("host", "port")}
        if "host" in defaults:
            out["host"] = defaults["host"]
        if "port" in defaults:
            out["port"] = defaults["port"]
    metadata = recipe.get("metadata") or {}
    if isinstance(metadata, dict) and metadata:
        out["metadata"] = metadata
        if "readiness_seconds" in metadata:
            out["readiness_seconds"] = metadata["readiness_seconds"]
        if isinstance(metadata.get("litellm"), dict):
            out["litellm"] = metadata["litellm"]
    # Placement/inline keys win over the recipe; the reference key itself is
    # kept so downstream code (and re-resolution in host views) can see it.
    for key, value in mdef.items():
        if key != "recipe":
            out[key] = value
    out["recipe"] = ref
    return out


def resolve_all(data: Dict[str, Any], config_dir: Path) -> Dict[str, Any]:
    """Resolve every ``recipe:`` reference in a v3 config's model blocks.

    Returns a new data dict (inputs are not mutated). v1/v2 configs and v3
    blocks without ``recipe:`` pass through untouched.
    """
    if data.get("version") != 3:
        return data
    models = data.get("models")
    if not isinstance(models, dict):
        return data
    out_models = {}
    changed = False
    for alias, mdef in models.items():
        new = resolve_model_block(mdef or {}, config_dir)
        if new is not mdef:
            changed = True
        out_models[alias] = new
    if not changed:
        return data
    out = dict(data)
    out["models"] = out_models
    return out


# --------------------------------------------------------------------------- #
# layout pins (render-time only -- repo recipes stay placement-agnostic)
# --------------------------------------------------------------------------- #
def layout_for_view(cfg) -> List[Tuple[str, List[int]]]:
    """The ``layout.placements`` for the active model on this config/host view.

    A single-host model pins the run pool's default address (``cfg.hosts[0]``
    -- the same address the ``--hosts`` flag passes), rank 0. A spanning
    model (``min_nodes: > 1``) pins each of its placement hosts, in config
    order, one rank each; rank 0 lands on the first host (sparkrun's
    head-node contract). A spanning model's run pool *is* its own placement:
    the ``--hosts`` flag passes exactly ``models.<m>.hosts`` (see
    ``converge.build_plan``), so the layout hosts match string-equal by
    construction -- no global ``install.hosts`` pool is required.

    Hosts are named the way sparkrun can resolve them: a host's explicit
    ``ip:`` when set, else its name (see ``Config.sparkrun_addresses``) --
    sparkrun's scheduler matches ``layout.placements`` / ``--hosts`` entries
    against cluster host IPs, not hostnames.
    """
    addr = dict(getattr(cfg, "sparkrun_addresses", {}) or {})

    def _a(name: str) -> str:
        return addr.get(name, name)

    m = cfg.model
    if not m:
        return []
    min_nodes = int(m.get("min_nodes", 1))
    if min_nodes <= 1:
        pool = [str(h) for h in (cfg.hosts or ["127.0.0.1"])]
        return [(_a(pool[0]), [0])]
    alias = str(cfg.active_alias or "")
    host_names = [str(h) for h in cfg.model_host_list(alias)]
    if len(host_names) < min_nodes:
        raise RecipeError(
            f"model '{alias}' spans {min_nodes} hosts but is placed on "
            f"{len(host_names)} ({host_names}) -- extend its hosts: list.")
    # The placement IS the run pool for a spanning model, so each name is
    # matched by construction (the host-order/count check above is the
    # real guard; it also fails earlier at load via _validate_recipe_spans).
    return [(_a(name), [i]) for i, name in enumerate(host_names[:min_nodes])]
