"""`spark-lab expose` — put any OpenAI-compatible engine behind the gateway.

One command for the "I started a model by hand, now serve it through
LiteLLM" workflow: probe the engine's /v1/models (from here; the engine's
LAN/tailnet address must be reachable), append a well-formed
``litellm.extra_models`` entry to config.yaml, and converge the gateways
(model workloads are never touched -- a gateway restart is triggered
automatically by the changed extra_models file).

Comment preservation matches the other config-rewriting verbs (``model
up/down``): a YAML round-trip, comments are not kept. Costs/vision flags:
edit the config afterwards; apply converges them.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import yaml

from ..core import config as config_mod
from . import apply as apply_cmd


def probe_engine(base_url: str, timeout: float = 5.0) -> list:
    """The ids an engine serves (GET <base>/v1/models). Raises on any failure;
    callers turn that into an actionable error."""
    with urllib.request.urlopen(base_url.rstrip("/") + "/v1/models", timeout=timeout) as r:
        return [m["id"] for m in json.load(r)["data"]]


def entry_for(served_model: str, address: str, port: int, public_name: str | None = None) -> dict:
    """A litellm extra_models entry (raw LiteLLM model_list shape)."""
    return {
        "model_name": public_name or served_model,
        "litellm_params": {
            "model": f"custom_openai/{served_model}",
            "api_base": f"http://{address}:{port}/v1",
            "api_key": "not-needed",
        },
    }


def add_extra_model(config_path: Path, entry: dict) -> None:
    """Append an extra_models entry to config.yaml (YAML round-trip)."""
    data = yaml.safe_load(config_path.read_text()) or {}
    lit = data.setdefault("litellm", {})
    models = lit.setdefault("extra_models", [])
    if any(m.get("model_name") == entry["model_name"] for m in models):
        raise ValueError(
            f"extra_models entry '{entry['model_name']}' already exists (edit the config or pick another --public-name)"
        )
    models.append(entry)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def run(args) -> int:
    cfg = config_mod.load(args.config)
    spec_name = args.host.split(":")[0]
    port = int(args.host.split(":")[1]) if ":" in args.host else int(args.port or 8000)
    try:
        spec = cfg.select_hosts([spec_name])[0]
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    address = spec.ip or spec.ssh_host or spec.name
    try:
        served = probe_engine(f"http://{address}:{port}")
    except Exception as e:  # noqa: BLE001 - network error == actionable message
        print(
            f"[ERROR] engine probe failed at http://{address}:{port}/v1/models: {e}\n"
            f"        is the engine up and reachable from here?",
            file=sys.stderr,
        )
        return 1
    served_model = args.served_model or served[0]
    if served_model not in served:
        print(
            f"[ERROR] engine serves {served}; '{served_model}' not among it (--served-model must match one)",
            file=sys.stderr,
        )
        return 1
    entry = entry_for(served_model, address, port, args.public_name)

    config_path = Path(cfg.config_path)
    if not getattr(args, "dry_run", False):
        try:
            add_extra_model(config_path, entry)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            return 1
    print(
        f"{'[dry-run] would add' if getattr(args, 'dry_run', False) else 'Added'} "
        f"gateway entry: {entry['model_name']} -> {entry['litellm_params']['api_base']}"
        f"{' (YAML round-trip: config comments were not preserved)' if not getattr(args, 'dry_run', False) else ''}"
    )
    if getattr(args, "dry_run", False):
        return 0

    # Converge gateways only: the extra_models.yaml change triggers a verified
    # litellm restart (converge); no model workload is launched or stopped.
    ns = SimpleNamespace(
        config=args.config,
        hosts=None,
        dry_run=False,
        no_model=True,
        restart_model=False,
        diff=False,
        verbose=getattr(args, "verbose", False),
        json=False,
        runtime=args.runtime,
    )
    return apply_cmd.run(ns)
