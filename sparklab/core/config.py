"""Load and validate the spark-lab config + resolve secrets from .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml


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

    # -- section accessors -------------------------------------------------
    @property
    def install(self) -> Dict[str, Any]:
        return self.data.get("install", {})

    @property
    def model(self) -> Dict[str, Any]:
        return self.data.get("model", {})

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
