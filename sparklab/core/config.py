"""Load and validate the spark-lab config + resolve secrets from .env.

Supports two schema versions (ADR 0004):

* **v1** -- a single top-level ``model:`` block (no ``version:`` key). The
  legacy shape; still fully supported and renders byte-identically.
* **v2** -- a ``version: 2`` with a keyed ``models:`` map (multi-model +
  ``active``), a top-level ``images:`` map, and ``profile:``/``profiles:``
  overrides. Strictly additive: every v1 field keeps its meaning.

The rest of the engine reads a few derived properties off ``Config`` --
``model`` (the *active* model definition), ``recipe_name`` (the active alias),
``image(key)`` (image resolution with precedence), and ``effective_params()``.
Those are version-agnostic, so render/converge never branch on the schema.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

# Default image references. These equal the values the legacy (v1) templates
# hard-coded / defaulted to, so a v1 config with no overrides renders
# byte-identically (the R3 regression the golden tests assert).
IMAGE_DEFAULTS: Dict[str, str] = {
    "litellm": "docker.litellm.ai/berriai/litellm:main-stable",
    "db": "pgvector/pgvector:pg16",
    "redis": "redis:7-alpine",
    "prometheus": "prom/prometheus",
    "grafana": "grafana/grafana",
    "node_exporter": "prom/node-exporter:latest",
    "dcgm_exporter": "utkuozdemir/nvidia_gpu_exporter:latest",
    "cadvisor": "gcr.io/cadvisor/cadvisor:latest",
    "gpu_textfile": "ubuntu:24.04",
}

# Every container image the stack can pull, by key. `check images` enumerates
# these so the full image set is explicit and validated (Objective 8).
IMAGE_KEYS: Tuple[str, ...] = tuple(IMAGE_DEFAULTS.keys())


def load_dotenv(path: Path) -> Dict[str, str]:
    """Parse a simple ``KEY=VALUE`` .env file into a dict.

    Lines that are blank or start with ``#`` are skipped. Surrounding quotes on
    the value are stripped. This is intentionally minimal -- no interpolation.
    """
    env: Dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env


class Config:
    """A thin wrapper over the parsed config.yaml plus resolved secret values."""

    def __init__(self, data: Dict[str, Any], config_path: Path, env: Dict[str, str]):
        self.data = data or {}
        self.config_path = Path(config_path)
        self.repo_root = self.config_path.parent
        self.env = env
        self._is_v2 = "models" in self.data or self.data.get("version") == 2
        self._active_alias, self._active_model = self._select_active()

    # -- version + active-model selection ----------------------------------
    @property
    def is_v2(self) -> bool:
        return self._is_v2

    def _select_active(self) -> Tuple[str, Dict[str, Any]]:
        """Return ``(alias, model_dict)`` for the model that should be live.

        v1: the single ``model:`` block, alias = its ``recipe_name``.
        v2: ``active_models:`` (list) wins; else the one ``active: true``.
        """
        if not self._is_v2:
            m = self.data.get("model") or {}
            return str(m.get("recipe_name") or "model"), m
        models = self.data.get("models") or {}
        if not models:
            raise ValueError("v2 config has an empty 'models:' map")
        active_models = self.data.get("active_models")
        if active_models:
            alias = str(active_models[0])
            if alias not in models:
                raise ValueError(f"active_models[0] '{alias}' is not in 'models:'")
            return alias, models[alias] or {}
        active = [a for a, m in models.items() if (m or {}).get("active")]
        if len(active) == 1:
            return active[0], models[active[0]] or {}
        if len(active) == 0:
            raise ValueError("v2 config: no model has 'active: true' (or set active_models:)")
        raise ValueError(f"v2 config: multiple active models {active}; set exactly one "
                         f"(or use active_models:)")

    # -- active model ------------------------------------------------------
    @property
    def model(self) -> Dict[str, Any]:
        """The *active* model's definition (v1: the single model block)."""
        return self._active_model

    @property
    def active_alias(self) -> str:
        return self._active_alias

    @property
    def models(self) -> Dict[str, Any]:
        if not self._is_v2:
            return {self._active_alias: (self.data.get("model") or {})}
        return self.data.get("models") or {}

    @property
    def active_resources(self) -> Dict[str, Any]:
        return self._active_model.get("resources") or {}

    # -- images -------------------------------------------------------------
    @property
    def images(self) -> Dict[str, Any]:
        return self.data.get("images") or {}

    @property
    def profile(self) -> str:
        return str(self.data.get("profile") or "prod")

    @property
    def profiles(self) -> Dict[str, Any]:
        return self.data.get("profiles") or {}

    def _v1_image_field(self, key: str) -> Optional[str]:
        v1 = {
            "litellm": self.litellm.get("image"),
            "db": self.db().get("image"),
            "redis": self.redis().get("image"),
            "prometheus": self.prometheus().get("image"),
            "grafana": self.grafana().get("image"),
        }
        return v1.get(key)

    def _profile_image(self, key: str) -> Optional[str]:
        return ((self.profiles.get(self.profile) or {}).get("images") or {}).get(key)

    def image(self, key: str, default: Optional[str] = None) -> str:
        """Resolve a shared-stack image by key.

        Precedence (highest first): env ``SPARKLAB_IMAGE_<KEY>`` > active
        ``profile:`` override > v2 ``images:`` map > v1 per-service field >
        historical default. Un-overridden v1 therefore renders byte-identically.
        """
        default = default or IMAGE_DEFAULTS.get(key, "")
        env_val = os.environ.get(f"SPARKLAB_IMAGE_{key.upper()}", "").strip()
        if env_val:
            return env_val
        prof = self._profile_image(key)
        if prof:
            return prof
        if self.images.get(key):
            return self.images[key]
        v1 = self._v1_image_field(key)
        if v1:
            return v1
        return default

    def image_model(self) -> str:
        """The active model's image (env ``SPARKLAB_IMAGE_MODEL`` > profile > model.image)."""
        env_val = os.environ.get("SPARKLAB_IMAGE_MODEL", "").strip()
        if env_val:
            return env_val
        prof = self._profile_image("model")
        if prof:
            return prof
        return self.model.get("image") or ""

    def resolved_images(self) -> Dict[str, str]:
        """Every image the active deploy can pull, resolved (for `check images`).

        Includes the active model's image + each shared-stack image that is
        actually enabled (redis / monitoring) for this config.
        """
        out: Dict[str, str] = {}
        out["model"] = self.image_model()
        out["litellm"] = self.image("litellm")
        out["db"] = self.image("db")
        if self.redis().get("enabled", False):
            out["redis"] = self.image("redis")
        if self.monitoring.get("enabled", True):
            for k in ("prometheus", "grafana", "node_exporter", "dcgm_exporter",
                      "cadvisor", "gpu_textfile"):
                out[k] = self.image(k)
        return out

    # -- params -------------------------------------------------------------
    def effective_params(self) -> Dict[str, Any]:
        """``model.params`` with ``resources.mem_fraction_static`` taking precedence.

        A v1 config carries the memory ceiling inside ``params``; a v2 config may
        move it to ``resources``. ``resources`` wins when present, else ``params``.
        """
        params = dict(self.model.get("params") or {})
        mfs = self.active_resources.get("mem_fraction_static")
        if mfs is not None:
            params["mem_fraction_static"] = mfs
        return params

    # -- section accessors -------------------------------------------------
    @property
    def install(self) -> Dict[str, Any]:
        return self.data.get("install", {})

    @property
    def remote(self) -> Dict[str, Any]:
        """The ``install.remote`` block (empty when the node is local).

        Keys: ``host`` (SSH target), optional ``user`` / ``port`` /
        ``identity_file`` / ``repo_dir`` (the node's spark-lab checkout).
        """
        return self.install.get("remote") or {}

    @property
    def is_remote(self) -> bool:
        return bool(self.remote.get("host"))

    @property
    def litellm(self) -> Dict[str, Any]:
        return self.data.get("litellm", {})

    @property
    def monitoring(self) -> Dict[str, Any]:
        return self.data.get("monitoring", {})

    @property
    def network(self) -> Dict[str, Any]:
        return self.data.get("network", {})

    # -- derived values ----------------------------------------------------
    @property
    def install_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.install.get("install_dir", "~/AI"))))

    @property
    def install_dir_raw(self) -> str:
        """``install.install_dir`` exactly as written (no local expanduser).

        In remote mode this string (e.g. ``~/AI``) refers to the *target
        node's* filesystem; it must be expanded there, not on the operator's
        machine. Local mode keeps using :attr:`install_dir` (expanded here).
        """
        return str(self.install.get("install_dir", "~/AI"))

    @property
    def remote_repo_dir(self) -> str:
        """The target node's spark-lab checkout (state + upgrade), raw form."""
        return str(self.remote.get("repo_dir") or "~/spark-lab")

    def node_path(self, rel: str, home: Optional[str] = None) -> str:
        """A path under the install dir, *as it exists on the target node*.

        Local: an absolute path on this machine (byte-identical to the
        historical ``str(Path(cfg.install_dir) / rel)``). Remote: the node-side
        path with ``~`` expanded against the remote ``home`` when given
        (``home`` comes from ``runtime.home_path()``).
        """
        if self.is_remote:
            base = self.install_dir_raw
            if base.startswith("~") and home:
                # Expand against the REMOTE home (plain ~ / ~/... forms).
                base = home + base[1:] if base.startswith("~/") else home
            return f"{base.rstrip('/')}/{str(rel).lstrip('/')}"
        return str(self.install_dir / rel)

    @property
    def hosts(self):
        return self.install.get("hosts", ["127.0.0.1"])

    @property
    def is_cluster(self) -> bool:
        return len(self.hosts) > 1

    @property
    def cluster_name(self) -> str:
        return self.install.get("cluster_name", "mylab")

    @property
    def recipe_name(self) -> str:
        if self._is_v2:
            return self._active_alias
        return self.model.get("recipe_name", "model")

    @property
    def deploy_dir(self) -> Path:
        return self.repo_root / "deploy"

    @property
    def state_dir(self) -> Path:
        return self.repo_root / ".sparklab-state"

    def secret(self, name: str | None) -> str:
        """Return the value of a secret by env-var name (from .env or env)."""
        if not name:
            return ""
        return self.env.get(name, "") or os.environ.get(name, "")

    def db(self) -> Dict[str, Any]:
        return self.litellm.get("db", {})

    def redis(self) -> Dict[str, Any]:
        return self.litellm.get("redis", {"enabled": False})

    def prometheus(self) -> Dict[str, Any]:
        return self.monitoring.get("prometheus", {})

    def grafana(self) -> Dict[str, Any]:
        return self.monitoring.get("grafana", {})

    def tailscale(self) -> Dict[str, Any]:
        return self.network.get("tailscale", {"enabled": True})

    def cloudflare(self) -> Dict[str, Any]:
        return self.network.get("cloudflare", {"enabled": False})

    def discovery(self) -> Dict[str, Any]:
        return self.data.get("discovery", {}) or {}

    @property
    def model_api_base(self) -> str:
        host = self.litellm.get("model_api_base_host", "host.docker.internal")
        port = self.model.get("port", 30000)
        return f"http://{host}:{port}/v1"


def load(config_path: str | Path) -> Config:
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"config not found: {config_path}. Run `spark-lab init` first."
        )
    data = yaml.safe_load(config_path.read_text()) or {}
    env = load_dotenv(config_path.parent / ".env")
    return Config(data, config_path, env)
