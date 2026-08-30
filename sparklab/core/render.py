"""Render templates into a local ``deploy/`` tree that mirrors the target layout.

``.j2`` files are rendered with Jinja2 (strict undefined, so a typo fails fast).
Every other file is copied verbatim -- this is how we ship the Grafana dashboard
JSONs, which contain ``{{``-style sequences that would otherwise break templating.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

import yaml as yaml_mod
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import config as config_mod
from . import recipes as recipe_mod

# Base executor_config the recipe template emits when a model declares no
# `executor_config:` overrides (kept static in the template for byte-identical
# default renders; overrides replace whole keys in the merged dict instead).
EXECUTOR_CONFIG_BASE: Dict[str, object] = {
    "ipc": "host",
    "shm_size": "32g",
    "privileged": True,
    "cap_add": ["SYS_PTRACE"],
    "security_opt": ["seccomp=unconfined"],
}


def yaml_block(d: dict, indent: int = 2) -> str:
    """A YAML mapping as indented text (for embedding under a template key).

    ``safe_dump`` (sort_keys=False) keeps key order and emits valid YAML for
    scalars/lists/dicts alike; every line is padded to *indent* spaces.
    """
    text = yaml_mod.safe_dump(
        d, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return "\n".join(" " * indent + line for line in text.rstrip("\n").splitlines())


# (template source under templates/, target path under install_dir)
# The recipe target embeds the recipe name.
def target_mapping(cfg: config_mod.Config) -> List[Tuple[str, str]]:
    recipe = cfg.recipe_name
    role = cfg.monitoring_role()
    entries = [("docker-compose.yaml.j2", "litellm/docker-compose.yml")]
    if cfg.control_plane_enabled():
        # The gateway's own config files. A control-plane-off host still gets
        # the compose file (it carries the observability services) but none of
        # these three.
        entries += [
            ("litellm_config.yaml.j2", "litellm/config.yaml"),
            ("litellm_model_config.yaml.j2", "litellm/model_config.yaml"),
            ("litellm.env.j2", "litellm/.env"),
        ]
    if role == "full":
        # Central observability stack: prometheus config + the grafana
        # provisioning/dashboards that only a 'full' host's grafana consumes.
        entries += [
            ("prometheus.yml.j2", "litellm/prometheus.yml"),
            ("grafana/provisioning/datasources/prometheus.yml.j2",
             "litellm/grafana/provisioning/datasources/prometheus.yml"),
            ("grafana/provisioning/dashboards/dashboards.yml.j2",
             "litellm/grafana/provisioning/dashboards/dashboards.yml"),
            ("grafana/dashboards/sglang-dashboard.json",
             "litellm/grafana/dashboards/sglang-dashboard.json"),
            ("grafana/dashboards/spark-host-overview.json",
             "litellm/grafana/dashboards/spark-host-overview.json"),
        ]
    if role in ("full", "exporters"):
        # The gpu_textfile sidecar script (consumed by the exporters' node
        # textfile collector) runs on every host that carries exporters.
        entries.append(("scripts/nvidia-gpu-textfile.sh", "litellm/scripts/nvidia-gpu-textfile.sh"))
    # The recipe file is rendered only when a model is actually active for this
    # config/host view (a v3 host may serve no model at all -- control plane only).
    if recipe:
        entries.insert(0, ("sparkrun_recipe.yaml.j2", f"sparkrun/recipes/{recipe}.yaml"))
    return entries


def _env_line(key: str, value: str) -> str:
    """One rendered `env:` line (2-space indented).

    Dumped in *mapping-value* context (via a one-key dict) so PyYAML's
    document-end marker (appended to bare top-level scalars) can never leak
    into the line. Empty values render as "" -- the historical hardcoded
    template style, which the live on-disk recipes carry."""
    if "\n" in str(value):
        raise ValueError(
            f"env value for '{key}' must be a single line (got {value!r})")
    if value == "":
        return f'  {key}: ""'
    dumped = yaml_mod.safe_dump({key: str(value)}, default_flow_style=False,
                                allow_unicode=True, sort_keys=False).rstrip("\n")
    return "  " + dumped


def _env_lines(model_env: Dict[str, Any], hf_token: str) -> List[str]:
    """The rendered `env:` block, line by line (order: base keys, model keys,
    injected token last). The HF_TOKEN line keeps its historical explicit
    double quotes + gated-model comment."""
    lines: List[str] = [_env_line("PYTORCH_CUDA_ALLOC_CONF", ""),
                        _env_line("PYTORCH_ALLOC_CONF", "")]
    by_key = {l.split(":")[0].strip(): i for i, l in enumerate(lines)}
    for k, v in (model_env or {}).items():
        k = str(k)
        if k in by_key:
            lines[by_key[k]] = _env_line(k, str(v))
        else:
            by_key[k] = len(lines)
            lines.append(_env_line(k, str(v)))
    if hf_token:
        lines.append("  # Gated model -- the token is injected at deploy time from your .env.")
        lines.append(f'  HF_TOKEN: "{hf_token}"')
    return lines


def build_context(cfg: config_mod.Config) -> dict:
    """Assemble the flat context the templates read from."""
    model, litellm = cfg.model, cfg.litellm
    monitoring = cfg.monitoring
    db = cfg.db()
    redis = cfg.redis()
    executor_overrides = model.get("executor_config") or {}
    executor_final: Dict[str, object] = dict(EXECUTOR_CONFIG_BASE)
    executor_final.update(executor_overrides)
    hf_token = cfg.secret(model.get("hf_token_env"))
    # Placement pin is a v3 (cluster) feature: v1/v2 renders stay byte-frozen
    # (their single-node placement needs no pin, and legacy goldens must not
    # move).
    layout_placements = recipe_mod.layout_for_view(cfg) if cfg.is_v3 else []
    return {
        "cfg": cfg.data,
        "install": cfg.install,
        "model": model,
        "litellm": litellm,
        "monitoring": monitoring,
        "network": cfg.network,
        # flat, commonly-used values
        "instance": monitoring.get("instance_label", "spark"),
        "api_base": cfg.model_api_base,
        "model_port": model.get("port", 30000),
        "gateway_port": litellm.get("port", 4000),
        "model_name": litellm.get("model_name", "my-spark-model"),
        "model_info": litellm.get("model_info", {}),
        "model_settings": {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
                           **(litellm.get("model_settings") or {})},
        # Gateway-facing model_list entries (implicit central serving):
        # one per active model with a running host; api_base local when the
        # model runs on this host, remote (tailnet/LAN address) otherwise.
        "served_models": cfg.serving_entries(),
        "hf_model": model.get("hf_model", ""),
        "model_host": model.get("host", "0.0.0.0"),
        "min_nodes": model.get("min_nodes", 1),
        "runtime": model.get("runtime", "sglang"),
        "serve_command": str(model.get("serve_command") or "").rstrip(),
        "params": cfg.effective_params(),
        "extra_flags": model.get("extra_flags", []),
        "flag_map": model.get("flag_map", {}),
        "model_image": cfg.image_model(),
        "hf_token": cfg.secret(model.get("hf_token_env")),
        # Optional per-model recipe overrides (empty by default -> byte-identical
        # renders): executor_config keys added to / replacing the base block,
        # extra container env vars, and a pinned HF checkpoint revision. The
        # *_final dicts merge base + overrides so the rendered recipe never
        # carries duplicate YAML keys.
        "executor_overrides": executor_overrides,
        "executor_is_base": bool(executor_overrides)
        and executor_overrides == EXECUTOR_CONFIG_BASE,
        "executor_config_final": executor_final,
        "extra_env": model.get("env") or {},
        # Pre-serve hook commands (run inside each container before serve). Some
        # images apply required patches only at container runtime (e.g. the GB10
        # persistent_topk disable in glm53-flash-exl3); a blanked entrypoint skips
        # them, so the recipe carries them as pre_exec and they must survive render.
        "pre_exec": model.get("pre_exec") or [],
        "env_lines": _env_lines(model.get("env") or {}, hf_token),
        "model_metadata": model.get("metadata") or {},
        "model_revision": str(model.get("model_revision") or ""),
        # Placement pin rendered into the recipe (repo recipes stay
        # placement-agnostic; direct sparkrun users get scheduler placement).
        "layout_placements": layout_placements,
        # Defaults keep an omitted key-name from silently rendering an EMPTY
        # secret (an unauthenticated / broken gateway instead of a clear error).
        "master_key": cfg.secret(litellm.get("master_key_env", "LITELLM_MASTER_KEY")),
        "salt_key": cfg.secret(litellm.get("salt_key_env", "LITELLM_SALT_KEY")),
        "db_user": db.get("user", "litellm"),
        "db_password": cfg.secret(db.get("password_env", "LITELLM_DB_PASSWORD")),
        "db_name": db.get("db", "litellm"),
        "db_image": cfg.image("db"),
        "redis_enabled": bool(redis.get("enabled", False)),
        "redis_image": cfg.image("redis"),
        "redis_port": redis.get("port", 6379),
        "monitoring_enabled": bool(monitoring.get("enabled", True)),
        "monitoring_role": cfg.monitoring_role(),
        "monitoring_exporters_enabled": cfg.monitoring_role() in ("full", "exporters"),
        "monitoring_stack_enabled": cfg.monitoring_role() == "full",
        "control_plane_enabled": cfg.control_plane_enabled(),
        "remote_scrape_targets": cfg.remote_scrape_targets(),
        "prometheus_image": cfg.image("prometheus"),
        "prometheus_port": cfg.prometheus().get("port", 9090),
        "prometheus_retention": cfg.prometheus().get("retention", "15d"),
        "grafana_image": cfg.image("grafana"),
        "grafana_port": cfg.grafana().get("port", 3000),
        "litellm_image": cfg.image("litellm"),
        "node_exporter_image": cfg.image("node_exporter"),
        "dcgm_exporter_image": cfg.image("dcgm_exporter"),
        "cadvisor_image": cfg.image("cadvisor"),
        "gpu_textfile_image": cfg.image("gpu_textfile"),
        "grafana_admin_password": cfg.secret(
            cfg.grafana().get("admin_password_env", "GRAFANA_ADMIN_PASSWORD")),
        "dashboards": monitoring.get("dashboards", []),
        "tailscale_enabled": bool(cfg.tailscale().get("enabled", True)),
        "cloudflare_enabled": bool(cfg.cloudflare().get("enabled", False)),
        "cf_token": cfg.secret(cfg.cloudflare().get("tunnel_token_env")),
        "cf_hostname": cfg.cloudflare().get("public_hostname", ""),
    }


def yaml_scalar(v):
    """Render ``v`` as a YAML line scalar that round-trips to the SAME value.

    String values that YAML would otherwise parse as something else (the
    JSON-string ``speculative_config`` would become a flow mapping, turning
    a string into a dict on the node -- which then reaches the shell as a
    mangled ``str(dict)``) are emitted JSON-quoted. JSON quoting is valid
    YAML quoting. Non-strings are dumped by PyYAML (a bare ``2`` stays a
    bare int; ``True`` would have rendered as the string "None"/"True").
    """
    if isinstance(v, str):
        if "\n" in v:
            return json.dumps(v)
        try:
            probe = yaml_mod.safe_load(v)
        except yaml_mod.YAMLError:
            return json.dumps(v)
        if probe != v or not isinstance(probe, str):
            return json.dumps(v)
        return v
    dumped = yaml_mod.safe_dump(v, default_flow_style=True, width=4096).rstrip("\n")
    if dumped.endswith("..."):   # PyYAML's document-end marker on scalar dumps
        dumped = dumped[:-3].rstrip()
    return dumped


def render(cfg: config_mod.Config, deploy_dir: Path) -> Dict[str, bytes]:
    """Render all templates to ``deploy_dir`` and return {target_rel: bytes}."""
    deploy_dir = Path(deploy_dir)
    deploy_dir.mkdir(parents=True, exist_ok=True)
    # Templates live next to the code (repo_root/templates), independent of where
    # the user's config.yaml happens to be.
    tmpl_root = Path(__file__).resolve().parent.parent / "templates"

    jenv = Environment(
        loader=FileSystemLoader(str(tmpl_root)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    jenv.filters["yaml_block"] = yaml_block
    jenv.filters["yaml_scalar"] = yaml_scalar
    ctx = build_context(cfg)
    rendered: Dict[str, bytes] = {}

    for src_rel, tgt_rel in target_mapping(cfg):
        src = tmpl_root / src_rel
        if not src.is_file():
            raise FileNotFoundError(f"missing template: {src_rel}")
        dest = deploy_dir / tgt_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src_rel.endswith(".j2"):
            text = jenv.get_template(src_rel).render(**ctx)
            dest.write_text(text)
            rendered[tgt_rel] = text.encode("utf-8")
        else:
            data = src.read_bytes()
            dest.write_bytes(data)
            rendered[tgt_rel] = data
    return rendered
