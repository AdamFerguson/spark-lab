"""Declared config schema shape + the v1 -> v2 upgrade (ADR 0004).

``upgrade_to_v2`` is the pure, render-invariant transform: it takes a v1 config
dict (a single ``model:`` block) and returns the equivalent v2 dict
(``version: 2``, keyed ``models:`` with ``active``, and an explicit ``images:``
map). Rendering over the result is byte-identical to rendering over the original
v1 config -- that is the R3 regression the parity/golden tests assert.
"""
from __future__ import annotations

from typing import Any, Dict


def is_v2(data: Dict[str, Any]) -> bool:
    """True if this config dict is schema v2."""
    return "models" in (data or {}) or (data or {}).get("version") == 2


def upgrade_to_v2(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new v2 config dict equivalent to the given v1 config.

    Preserves every value: the single ``model:`` block becomes ``models.<alias>``
    (alias = its ``recipe_name``) with ``active: true``, and the v1 per-service
    image fields are lifted into an explicit ``images:`` map. No field is renamed
    or dropped, so the v2 form renders identically to the v1 original.
    """
    src = dict(data or {})
    model = dict(src.get("model") or {})
    alias = str(model.get("recipe_name") or "model")
    model.setdefault("active", True)

    out = dict(src)
    out["version"] = 2
    out["models"] = {alias: model}
    out.pop("model", None)

    # Lift the v1 per-service image fields up into the explicit `images:` map.
    lit = dict(src.get("litellm") or {})
    mon = dict(src.get("monitoring") or {})
    db = lit.get("db") or {}
    redis = lit.get("redis") or {}
    prom = mon.get("prometheus") or {}
    graf = mon.get("grafana") or {}
    images: Dict[str, Any] = {}
    for key, block in (("litellm", lit), ("db", db), ("redis", redis),
                       ("prometheus", prom), ("grafana", graf)):
        if block.get("image"):
            images[key] = block["image"]
    if images:
        out["images"] = images
    return out
