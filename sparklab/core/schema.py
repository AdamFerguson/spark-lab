"""Declared config schema shape + the v1 -> v2 -> v3 upgrades (ADR 0004, ADR 0008).

``upgrade_to_v2`` is the pure, render-invariant v1 -> v2 transform (single
``model:`` block -> keyed ``models:`` + explicit ``images:``). Rendering over its
result is byte-identical to rendering over the original v1 config -- the R3
regression the parity/golden tests assert.

``upgrade_to_v3`` (ADR 0008) adds the multi-host shape: a top-level ``hosts:``
list replaces ``install.remote`` (one host entry synthesized from it, when
present) and ``install.hosts`` (dropped -- in v3 the model always runs on the
host being converged, so sparkrun placement is per-host local). ``hosts:`` and
``models.<m>.hosts`` / ``host_overrides`` are additive; every existing value is
preserved, so a single-node v2 config migrates to an equivalent v3 config.
"""
from __future__ import annotations

import socket
from typing import Any, Dict, Optional


def is_v2(data: Dict[str, Any]) -> bool:
    """True if this config dict is schema v2."""
    return "models" in (data or {}) or (data or {}).get("version") == 2


def is_v3(data: Dict[str, Any]) -> bool:
    """True if this config dict is schema v3 (multi-host cluster config)."""
    return (data or {}).get("version") == 3


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


def _v2_base(data: Dict[str, Any]) -> Dict[str, Any]:
    """The v2 form of any supported config (v1 upgraded, v2 as-is)."""
    if is_v2(data):
        return dict(data)
    return upgrade_to_v2(data)


def upgrade_to_v3(data: Dict[str, Any], host_name: Optional[str] = None,
                  ssh: Optional[str] = None) -> Dict[str, Any]:
    """Return a new v3 config dict equivalent to the given v1/v2 config.

    A legacy config describes exactly one node (this one). The transform:

    * adds ``version: 3`` and a top-level ``hosts:`` list with ONE entry:
      ``name`` = ``install.name`` (or the given / current hostname), and
      ``ssh``/``remote: true`` when the config carried ``install.remote``
      (remote operator mode) or a ``ssh`` is given. A host entry without
      ``ssh`` is local-only.
    * drops ``install.remote`` (replaced by the host entry) and
      ``install.hosts`` (v3 model placement is per-host local; sparkrun gets
      ``--hosts 127.0.0.1`` on the converged host).

    ``models.<m>.hosts`` / ``host_overrides`` are left to the operator (all
    hosts by default), so the migrated config converges the same node the v2
    config did.
    """
    base = _v2_base(data)
    if is_v3(base):
        return base

    out = dict(base)
    out["version"] = 3
    install = dict(out.get("install") or {})
    remote = dict(install.pop("remote", None) or {})
    install.pop("hosts", None)   # dropped in v3 (per-host local placement)

    name = str(host_name or install.get("name") or "")
    if not name:
        try:
            name = socket.gethostname()
        except OSError:
            name = "node"
    spec: Dict[str, Any] = {"name": name}
    ssh_target = ssh or remote.get("host")
    if ssh_target:
        spec["ssh"] = str(ssh_target)
        spec["remote"] = True
        for extra in ("user", "port", "identity_file"):
            if remote.get(extra) is not None:
                spec[extra] = remote[extra]
        if remote.get("repo_dir"):
            install["repo_dir"] = remote["repo_dir"]
    out["install"] = install
    out["hosts"] = [spec]
    return out
