"""Render templates into a local ``deploy/`` tree that mirrors the target layout.

``.j2`` files are rendered with Jinja2 (strict undefined, so a typo fails fast).
Every other file is copied verbatim -- this is how we ship the Grafana dashboard
JSONs, which contain ``{{``-style sequences that would otherwise break templating.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
    text = yaml_mod.safe_dump(d, sort_keys=False, default_flow_style=False, allow_unicode=True)
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
        # Externally-run models (config.yaml `litellm.extra_models`) AND the
        # generated zoo (llama-swap) entries: a second included model_list the
        # gateway serves but spark-lab never launches. Only rendered when there
        # is at least one, so the `include:` entry stays conditional.
        if cfg.litellm.get("extra_models") or cfg.swap_gateway_entries():
            entries.append(("litellm_extra_models.yaml.j2", "litellm/extra_models.yaml"))
    if role == "full":
        # Central observability stack: prometheus config + the grafana
        # provisioning/dashboards that only a 'full' host's grafana consumes.
        entries += [
            ("prometheus.yml.j2", "litellm/prometheus.yml"),
            (
                "grafana/provisioning/datasources/prometheus.yml.j2",
                "litellm/grafana/provisioning/datasources/prometheus.yml",
            ),
            (
                "grafana/provisioning/dashboards/dashboards.yml.j2",
                "litellm/grafana/provisioning/dashboards/dashboards.yml",
            ),
            ("grafana/dashboards/sglang-dashboard.json", "litellm/grafana/dashboards/sglang-dashboard.json"),
            ("grafana/dashboards/spark-host-overview.json", "litellm/grafana/dashboards/spark-host-overview.json"),
        ]
    if role in ("full", "exporters"):
        # The gpu_textfile sidecar script (consumed by the exporters' node
        # textfile collector) runs on every host that carries exporters.
        entries.append(("scripts/nvidia-gpu-textfile.sh", "litellm/scripts/nvidia-gpu-textfile.sh"))
    # The recipe file is rendered only when a model is actually active for this
    # config/host view (a v3 host may serve no model at all -- control plane only).
    if recipe:
        entries.insert(0, ("sparkrun_recipe.yaml.j2", f"sparkrun/recipes/{recipe}.yaml"))
    # Zoo (ADR-0010): every swap model placed on this host gets its node-side
    # recipe converged (llama-swap launches BY PATH) plus the llama-swap model
    # config and its systemd user unit (installed by `spark-lab zoo prepare`).
    # No layout pin: single-node, launched with --hosts (scheduler placement).
    for alias in cfg.swap_aliases():
        entries.append(("sparkrun_recipe.yaml.j2", f"sparkrun/recipes/{alias}.yaml"))
    if cfg.swap_aliases():
        entries.append(("llama_swap_config.yaml.j2", "llama-swap/config.yaml"))
        entries.append(("llama_swap_service.j2", "llama-swap/llama-swap.service"))
    return entries


def _env_line(key: str, value: str) -> str:
    """One rendered `env:` line (2-space indented).

    Dumped in *mapping-value* context (via a one-key dict) so PyYAML's
    document-end marker (appended to bare top-level scalars) can never leak
    into the line. Empty values render as "" -- the historical hardcoded
    template style, which the live on-disk recipes carry."""
    if "\n" in str(value):
        raise ValueError(f"env value for '{key}' must be a single line (got {value!r})")
    if value == "":
        return f'  {key}: ""'
    dumped = yaml_mod.safe_dump(
        {key: str(value)}, default_flow_style=False, allow_unicode=True, sort_keys=False
    ).rstrip("\n")
    return "  " + dumped


def _env_lines(model_env: Dict[str, Any], hf_token: str) -> List[str]:
    """The rendered `env:` block, line by line (order: base keys, model keys,
    injected token last). The HF_TOKEN line keeps its historical explicit
    double quotes + gated-model comment."""
    lines: List[str] = [_env_line("PYTORCH_CUDA_ALLOC_CONF", ""), _env_line("PYTORCH_ALLOC_CONF", "")]
    by_key = {ln.split(":")[0].strip(): i for i, ln in enumerate(lines)}
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


def _swap_ctx(cfg: config_mod.Config) -> dict:
    """llama-swap render context (ADR-0010): per-model cmd/cmdStop + ttl +
    proxy, plus the systemd user-unit ExecStart.

    cmd/cmdStop reference the node recipe by path with the ``{install_dir}``
    placeholder (expanded at write time, same seam as recipes) and run through
    ``bash -lc`` so the user daemon finds sparkrun on the login PATH. The unit
    uses systemd's ``%h`` for home (ExecStart does not expand ``~``).
    """
    from . import converge as converge_mod  # local import: no import cycle

    out = {"swap_entries": [], "swap_healthcheck_timeout": 600, "swap_unit_execstart": ""}
    aliases = cfg.swap_aliases()
    if not aliases:
        return out
    spec = cfg.select_hosts([cfg.view_host])[0]
    addr = spec.sparkrun_address
    sparkrun = str(cfg.swap().get("sparkrun_bin") or "sparkrun")
    entries: List[dict] = []
    readiness_max = 600
    for alias in aliases:
        mdef = (cfg.data.get("models") or {}).get(alias) or {}
        sw = mdef.get("swap") or {}
        readiness = int(mdef.get("readiness_seconds", 600))
        readiness_max = max(readiness_max, readiness)
        recipe_path = "{install_dir}/sparkrun/recipes/" + alias + ".yaml"
        cmds = converge_mod.swap_cmds(sparkrun, recipe_path, addr, cfg.swap_gateway_name(alias, mdef))
        name = cfg.swap_gateway_name(alias, mdef)
        entries.append(
            {
                "alias": alias,
                "model_id": name,
                "name": str(sw.get("display_name") or name),
                "addr": addr,
                "port": int(mdef.get("port", 30000)),
                "served": cfg.swap_served_id(mdef) or name,
                "ttl": cfg.swap_ttl(alias, mdef),
                "unload_timeout": int(sw.get("unload_timeout", 60)),
                "aliases": [str(a) for a in (sw.get("aliases") or [])],
                "readiness": readiness,
                **cmds,
            }
        )
    out["swap_entries"] = entries
    out["swap_healthcheck_timeout"] = readiness_max + 120  # cold-load headroom
    raw = cfg.install_dir_raw.rstrip("/")
    bin_raw = str(cfg.swap().get("bin") or (raw + "/bin/llama-swap"))
    conf = raw + "/llama-swap/config.yaml"

    def _sysd(p: str) -> str:
        return "%h" + p[1:] if p.startswith("~") else p

    out["swap_unit_execstart"] = f"{_sysd(bin_raw)} --config {_sysd(conf)} --listen {cfg.swap_listen()}"
    return out


def build_context(cfg: config_mod.Config, zoo_model: Dict[str, Any] | None = None) -> dict:
    """Assemble the flat context the templates read from.

    ``zoo_model`` renders the recipe template FOR A SWAP MODEL (the config's
    active model is untouched: zoo models are inactive by validation, so their
    node-side recipes render with this override -- no layout pin, single-node,
    scheduler placement via --hosts at launch).
    """
    model, litellm = (zoo_model or cfg.model), cfg.litellm
    monitoring = cfg.monitoring
    db = cfg.db()
    redis = cfg.redis()
    executor_overrides = model.get("executor_config") or {}
    executor_final: Dict[str, object] = dict(EXECUTOR_CONFIG_BASE)
    executor_final.update(executor_overrides)
    hf_token = cfg.secret(model.get("hf_token_env"))
    # Placement pin is a v3 (cluster) feature for the ACTIVE model; zoo recipes
    # carry no pin (launched with --hosts, scheduler placement -- the same
    # form direct `sparkrun run` users get).
    layout_placements = [] if zoo_model else recipe_mod.layout_for_view(cfg)
    if zoo_model:
        params = dict(model.get("params") or {})
        mfs = (model.get("resources") or {}).get("mem_fraction_static")
        if mfs is not None:
            params["mem_fraction_static"] = mfs
        model_image = str(model.get("image") or "")
    else:
        params = cfg.effective_params()
        model_image = cfg.image_model()
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
        "model_settings": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, **(litellm.get("model_settings") or {})},
        # Gateway-facing model_list entries (implicit central serving):
        # one per active model with a running host; api_base local when the
        # model runs on this host, remote (tailnet/LAN address) otherwise.
        "served_models": cfg.serving_entries(),
        # Externally-run models spark-lab registers in the gateway but never
        # launches/stops: hand-written `litellm.extra_models` entries PLUS the
        # generated zoo (llama-swap) entries -- merged into one
        # litellm/extra_models.yaml (api_base = llama-swap, never an engine
        # port; ADR-0010).
        "has_extra_models": bool((litellm.get("extra_models") or []) or cfg.swap_gateway_entries()),
        "extra_models_yaml": (
            yaml_mod.safe_dump(
                {"model_list": (litellm.get("extra_models") or []) + cfg.swap_gateway_entries()},
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
            ).rstrip()
            if (litellm.get("extra_models") or cfg.swap_gateway_entries())
            else ""
        ),
        "hf_model": model.get("hf_model", ""),
        "model_host": model.get("host", "0.0.0.0"),
        "min_nodes": model.get("min_nodes", 1),
        "runtime": model.get("runtime", "sglang"),
        "serve_command": str(model.get("serve_command") or "").rstrip(),
        "params": cfg.effective_params(),
        "extra_flags": model.get("extra_flags", []),
        "flag_map": model.get("flag_map", {}),
        "model_image": cfg.image_model() if zoo_model is None else model_image,
        "hf_token": cfg.secret(model.get("hf_token_env")),
        # Optional per-model recipe overrides (empty by default -> byte-identical
        # renders): executor_config keys added to / replacing the base block,
        # extra container env vars, and a pinned HF checkpoint revision. The
        # *_final dicts merge base + overrides so the rendered recipe never
        # carries duplicate YAML keys.
        "executor_overrides": executor_overrides,
        "executor_is_base": bool(executor_overrides) and executor_overrides == EXECUTOR_CONFIG_BASE,
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
        "grafana_admin_password": cfg.secret(cfg.grafana().get("admin_password_env", "GRAFANA_ADMIN_PASSWORD")),
        "dashboards": monitoring.get("dashboards", []),
        "tailscale_enabled": bool(cfg.tailscale().get("enabled", True)),
        "cloudflare_enabled": bool(cfg.cloudflare().get("enabled", False)),
        "cf_token": cfg.secret(cfg.cloudflare().get("tunnel_token_env")),
        "cf_hostname": cfg.cloudflare().get("public_hostname", ""),
        # zoo / llama-swap (ADR-0010)
        **_swap_ctx(cfg),
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
    if dumped.endswith("..."):  # PyYAML's document-end marker on scalar dumps
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
    # Zoo node-side recipes render with a per-model context (the config's
    # active model is untouched; zoo models are inactive by validation).
    zoo_ctxs: Dict[str, dict] = {}
    for alias in cfg.swap_aliases():
        mdef = (cfg.data.get("models") or {}).get(alias) or {}
        zoo_ctxs[f"sparkrun/recipes/{alias}.yaml"] = build_context(cfg, zoo_model=mdef)
    rendered: Dict[str, bytes] = {}

    for src_rel, tgt_rel in target_mapping(cfg):
        src = tmpl_root / src_rel
        if not src.is_file():
            raise FileNotFoundError(f"missing template: {src_rel}")
        dest = deploy_dir / tgt_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src_rel.endswith(".j2"):
            text = jenv.get_template(src_rel).render(**(zoo_ctxs.get(tgt_rel, ctx)))
            dest.write_text(text)
            rendered[tgt_rel] = text.encode("utf-8")
        else:
            data = src.read_bytes()
            dest.write_bytes(data)
            rendered[tgt_rel] = data
    return rendered
