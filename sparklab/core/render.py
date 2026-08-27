"""Render templates into a local ``deploy/`` tree that mirrors the target layout.

``.j2`` files are rendered with Jinja2 (strict undefined, so a typo fails fast).
Every other file is copied verbatim -- this is how we ship the Grafana dashboard
JSONs, which contain ``{{``-style sequences that would otherwise break templating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import config as config_mod

# (template source under templates/, target path under install_dir)
# The recipe target embeds the recipe name.
def target_mapping(cfg: config_mod.Config) -> List[Tuple[str, str]]:
    recipe = cfg.recipe_name
    entries = [
        ("docker-compose.yaml.j2", "litellm/docker-compose.yml"),
        ("litellm_config.yaml.j2", "litellm/config.yaml"),
        ("litellm_model_config.yaml.j2", "litellm/model_config.yaml"),
        ("litellm.env.j2", "litellm/.env"),
        ("prometheus.yml.j2", "litellm/prometheus.yml"),
        ("grafana/provisioning/datasources/prometheus.yml.j2",
         "litellm/grafana/provisioning/datasources/prometheus.yml"),
        ("grafana/provisioning/dashboards/dashboards.yml.j2",
         "litellm/grafana/provisioning/dashboards/dashboards.yml"),
        ("grafana/dashboards/sglang-dashboard.json",
         "litellm/grafana/dashboards/sglang-dashboard.json"),
        ("grafana/dashboards/spark-host-overview.json",
         "litellm/grafana/dashboards/spark-host-overview.json"),
        ("scripts/nvidia-gpu-textfile.sh", "litellm/scripts/nvidia-gpu-textfile.sh"),
    ]
    # The recipe file is rendered only when a model is actually active for this
    # config/host view (a v3 host may serve no model at all -- control plane only).
    if recipe:
        entries.insert(0, ("sparkrun_recipe.yaml.j2", f"sparkrun/recipes/{recipe}.yaml"))
    return entries


def build_context(cfg: config_mod.Config) -> dict:
    """Assemble the flat context the templates read from."""
    model, litellm = cfg.model, cfg.litellm
    monitoring = cfg.monitoring
    db = cfg.db()
    redis = cfg.redis()
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
        "hf_model": model.get("hf_model", ""),
        "model_host": model.get("host", "0.0.0.0"),
        "min_nodes": model.get("min_nodes", 1),
        "runtime": model.get("runtime", "sglang"),
        "serve_command": model.get("serve_command", ""),
        "params": cfg.effective_params(),
        "extra_flags": model.get("extra_flags", []),
        "flag_map": model.get("flag_map", {}),
        "model_image": cfg.image_model(),
        "hf_token": cfg.secret(model.get("hf_token_env")),
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
