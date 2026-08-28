"""Load and validate the spark-lab config + resolve secrets from .env.

Supports three schema versions:

* **v1** -- a single top-level ``model:`` block (no ``version:`` key). The
  legacy shape; still fully supported and renders byte-identically.
* **v2** -- a ``version: 2`` with a keyed ``models:`` map (multi-model +
  ``active``), a top-level ``images:`` map, and ``profile:``/``profiles:``
  overrides. Strictly additive: every v1 field keeps its meaning. (ADR 0004)
* **v3** -- a ``version: 3`` with a top-level ``hosts:`` list: one config for
  the whole cluster. Each entry names a managed node (``name``), how to reach
  it (``ssh``, ``remote``), and may override any cluster-wide key for that
  host (deep-merged). ``models.<m>.hosts`` says where a model is served;
  ``models.<m>.host_overrides.<host>`` tailors the model per host. (ADR 0008)

The rest of the engine reads a few derived properties off ``Config`` --
``model`` (the *active* model definition), ``recipe_name`` (the active alias),
``image(key)`` (image resolution with precedence), and ``effective_params()``.
Those are version-agnostic, so render/converge never branch on the schema; in
v3 they run against a per-host **view** (see :meth:`Config.view_for`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# Host-entry keys that describe the connection, not config overrides. The rest
# of a host entry is a deep-merge override applied to the cluster-wide config.
HOST_CONN_KEYS = ("name", "ssh", "remote", "user", "port", "identity_file")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` (dicts merge; other values
    replace). Neither input is mutated."""
    out: Dict[str, Any] = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class HostSpec:
    """One managed node: connection identity + per-host config overrides."""

    def __init__(self, name: str, ssh: Optional[str] = None, remote: bool = False,
                 user: Optional[str] = None, port: Optional[int] = None,
                 identity_file: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None):
        self.name = str(name)
        self.ssh = ssh
        self.remote = bool(remote)
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.overrides = dict(overrides or {})

    @classmethod
    def from_entry(cls, entry: Dict[str, Any]) -> "HostSpec":
        overrides = {k: v for k, v in (entry or {}).items() if k not in HOST_CONN_KEYS}
        ssh = (entry or {}).get("ssh")
        remote = (entry or {}).get("remote")
        if remote is None:
            # No explicit flag: a host we can ssh to is remote by default; a host
            # without an ssh target is local-only.
            remote = bool(ssh)
        return cls(
            name=str((entry or {}).get("name") or ""),
            ssh=str(ssh) if ssh else None,
            remote=bool(remote),
            user=(entry or {}).get("user"),
            port=(entry or {}).get("port"),
            identity_file=(entry or {}).get("identity_file"),
            overrides=overrides,
        )

    @property
    def ssh_host(self) -> str:
        """The connection host (``user@host`` -> ``host``)."""
        return str(self.ssh or "").split("@", 1)[-1]

    def __repr__(self) -> str:   # pragma: no cover - debug aid
        return (f"HostSpec(name={self.name!r}, ssh={self.ssh!r}, remote={self.remote}, "
                f"overrides={sorted(self.overrides)})")


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

    def __init__(self, data: Dict[str, Any], config_path: Path, env: Dict[str, str],
                 _host: Optional[str] = None, _base: Optional["Config"] = None):
        self.data = data or {}
        self.config_path = Path(config_path)
        self.repo_root = self.config_path.parent
        self.env = env
        self._is_v2 = "models" in self.data or self.data.get("version") in (2, 3)
        self._is_v3 = self.data.get("version") == 3
        # Per-host view bookkeeping (v3): ``_host`` names the host this view is
        # for; ``_base`` is the base (cluster-wide) config the view was cut from.
        self._view_host = _host
        self._view_base = _base
        self._active_alias, self._active_model = self._select_active()

    # -- version + active-model selection ----------------------------------
    @property
    def is_v2(self) -> bool:
        return self._is_v2

    @property
    def is_v3(self) -> bool:
        """True for a base v3 cluster config or a per-host view of one."""
        return self._is_v3 or self._view_host is not None

    @property
    def view_host(self) -> Optional[str]:
        """The host this config is a view for (None for the base/legacy config)."""
        return self._view_host

    # -- v3 monitoring roles (ADR-0008 addendum) ----------------------------
    def monitoring_role(self) -> str:
        """This host's monitoring split: 'full' (default -- prometheus + grafana
        + exporters), 'exporters' (the GPU/host exporter sidecars only; scraped
        by a full host's central prometheus), or 'none' (no monitoring
        services -- also reachable via the legacy ``monitoring.enabled: false``).
        """
        mon = self.data.get("monitoring") or {}
        if not mon.get("enabled", True):
            return "none"
        role = str(mon.get("role", "full")).lower()
        if role not in ("full", "exporters", "none"):
            raise ValueError(f"monitoring.role must be 'full', 'exporters' or 'none' (got {role!r})")
        return role

    # -- v3 control-plane split (ADR-0008 addendum) -------------------------
    def control_plane_enabled(self) -> bool:
        """Whether this host runs the LiteLLM control plane (gateway + DB +
        Redis). Default True; ``control_plane: {enabled: false}`` makes the
        host observability-only (no gateway, no recipe registration)."""
        cp = self.data.get("control_plane")
        if cp is None:
            return True
        if isinstance(cp, bool):
            return cp
        if not isinstance(cp, dict) or "enabled" not in cp:
            raise ValueError(
                "control_plane must be a boolean or {enabled: <bool>} "
                f"(got {cp!r})")
        enabled = cp["enabled"]
        if isinstance(enabled, str):
            enabled = enabled.strip().lower()
            if enabled not in ("true", "false"):
                raise ValueError(f"control_plane.enabled must be a boolean (got {enabled!r})")
            enabled = enabled == "true"
        if not isinstance(enabled, bool):
            raise ValueError(f"control_plane.enabled must be a boolean (got {enabled!r})")
        return enabled

    def control_plane_conflicts(self) -> List[str]:
        """v3: human-readable problems with the per-host control-plane split.

        Two invariants:
        1. a host that serves the active model must keep the control plane
           on (the LiteLLM gateway is how that model is served);
        2. a host with the control plane off must still run something
           (monitoring role != 'none'), otherwise it converges to an empty
           stack.
        """
        if not self.is_v3 or self._view_base is not None:
            return []
        problems: List[str] = []
        for spec in self.host_specs:
            view = self.view_for(spec.name)
            if view.control_plane_enabled():
                continue
            if view.active_alias:
                problems.append(
                    f"host '{spec.name}' serves model '{view.active_alias}' but "
                    "control_plane is disabled there -- the gateway is how the "
                    "model is served; re-enable it (or move the model off the host)")
            elif view.monitoring_role() == "none":
                problems.append(
                    f"host '{spec.name}' would run nothing (control_plane disabled "
                    "and monitoring.role: none)")
        return problems

    def remote_scrape_targets(self) -> List[Dict[str, Any]]:
        """For this host's central prometheus: the other hosts whose
        ``monitoring.role`` is 'exporters', with the address to scrape them at
        (the host's ``ssh`` value -- resolvable on the node via Tailscale/LAN
        DNS) and the instance label to tag their metrics with.

        v3 only; legacy configs return [] (rendered prometheus.yml is
        byte-identical to the historical output).
        """
        base = self._view_base if self._view_base is not None else self
        if not base.is_v3:
            return []
        out: List[Dict[str, Any]] = []
        for spec in base.host_specs:
            if spec.name == self.view_host:
                continue
            other = base.view_for(spec.name)
            if other.monitoring_role() != "exporters":
                continue
            port = None
            if other.model:
                port = other.model.get("port", 30000)
            out.append({
                "name": spec.ssh_host or spec.name,
                "instance": (other.data.get("monitoring") or {}).get("instance_label", "spark"),
                "model_port": port,
            })
        return out

    def _select_active(self) -> Tuple[str, Dict[str, Any]]:
        """Return ``(alias, model_dict)`` for the model that should be live.

        v1: the single ``model:`` block, alias = its ``recipe_name``.
        v2: ``active_models:`` (list) wins; else the one ``active: true``.
        v3 host view: first restricted to the models that *serve this host*
        (``models.<m>.hosts`` -- unset means all hosts); a host with no model
        serving it converges control-plane-only (no recipe, no model workload).
        """
        if not self._is_v2:
            m = self.data.get("model") or {}
            return str(m.get("recipe_name") or "model"), m
        models = self.data.get("models") or {}
        if self._view_host is not None:
            models = {a: m for a, m in models.items() if self._serves(self._view_host, m)}
            if not models:
                return "", {}
        elif not models:
            raise ValueError("v2 config has an empty 'models:' map")
        active_models = self.data.get("active_models")
        if active_models:
            alias = str(active_models[0])
            if alias in models:
                return alias, models[alias] or {}
            if self._view_host is not None:
                active_models = None   # named active model serves other hosts; fall through
            else:
                raise ValueError(f"active_models[0] '{alias}' is not in 'models:'")
        active = [a for a, m in models.items() if (m or {}).get("active")]
        if len(active) == 1:
            return active[0], models[active[0]] or {}
        if len(active) == 0:
            if self._view_host is not None:
                return "", {}   # models serve this host but none is marked active
            if self._is_v3 and not self.data.get("active_models"):
                return "", {}   # v3 cluster with every model scaled down: valid
            raise ValueError("v2 config: no model has 'active: true' (or set active_models:)")
        if self._is_v3 and self._view_host is None:
            # Cluster base: several active models are fine when their host sets
            # are disjoint (each host serves exactly one). Per-host views do
            # the real picking. The base's own `model` property is then just a
            # representative (first in order) for cluster-wide bookkeeping.
            if self.active_host_conflicts():
                raise ValueError(
                    "v3 config: two active models would both serve the same host: "
                    f"{self._conflict_pairs()}")
            return active[0], models[active[0]] or {}
        raise ValueError(f"v2 config: multiple active models {active}; set exactly one "
                         f"(or use active_models:)")

    @staticmethod
    def _serves(host: str, mdef: Dict[str, Any]) -> bool:
        """True when the model serves ``host``. Missing ``hosts:`` = all hosts;
        an empty ``hosts: []`` = nowhere (fully scaled down)."""
        hosts = (mdef or {}).get("hosts")
        if hosts is None:
            return True
        return host in hosts

    def model_host_list(self, alias: str) -> List[str]:
        """The host names model ``alias`` serves (all hosts when the key is
        absent; none when it is an empty list)."""
        mdef = (self.data.get("models") or {}).get(alias) or {}
        hosts = mdef.get("hosts")
        all_names = [s.name for s in self.host_specs]
        if hosts is None:
            return list(all_names)
        return list(hosts)

    def active_host_conflicts(self) -> bool:
        """True when two active models would both serve the same host (v3)."""
        models = self.data.get("models") or {}
        active = [a for a, m in models.items() if (m or {}).get("active")]
        first = str((self.data.get("active_models") or [None])[0] or "")
        if first and first in models and first not in active:
            active.append(first)
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                if set(self.model_host_list(a)) & set(self.model_host_list(b)):
                    return True
        return False

    def _conflict_pairs(self) -> List[str]:
        models = self.data.get("models") or {}
        active = [a for a, m in models.items() if (m or {}).get("active")]
        first = str((self.data.get("active_models") or [None])[0] or "")
        if first and first in models and first not in active:
            active.append(first)
        pairs = []
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                shared = sorted(set(self.model_host_list(a)) & set(self.model_host_list(b)))
                if shared:
                    pairs.append(f"{a} & {b} on {', '.join(shared)}")
        return pairs

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
        """The ``install.remote`` block (v1/v2 remote operator mode).

        Keys: ``host`` (SSH target), optional ``user`` / ``port`` /
        ``identity_file`` / ``repo_dir`` (the node's spark-lab checkout).
        Superseded by the v3 ``hosts:`` list; kept so legacy configs keep
        working unchanged.
        """
        return self.install.get("remote") or {}

    @property
    def is_remote(self) -> bool:
        return bool(self.remote.get("host"))

    # -- v3 multi-host (ADR 0008) ------------------------------------------
    @property
    def host_specs(self) -> List[HostSpec]:
        """The managed nodes. v3: the ``hosts:`` list. v1/v2: one implicit host
        (this node -- local, or the single ``install.remote`` target)."""
        if self._view_base is not None:
            return self._view_base.host_specs
        if self._is_v3:
            entries = self.data.get("hosts") or []
            if not entries:
                raise ValueError("v3 config has an empty 'hosts:' list")
            return [HostSpec.from_entry(e) for e in entries]
        # legacy: the implicit single host
        name = str(self.install.get("name") or "node")
        if self.is_remote:
            return [HostSpec(name=name, ssh=str(self.remote.get("host")), remote=True,
                              user=self.remote.get("user"), port=self.remote.get("port"),
                              identity_file=self.remote.get("identity_file"),
                              overrides={})]
        return [HostSpec(name=name, remote=False)]

    def select_hosts(self, names: Optional[List[str]] = None) -> List[HostSpec]:
        """The hosts a command targets: all of them, or the named subset.

        ``names`` are host names from the config's ``hosts:`` list (comma-split
        happens in the CLI layer). Unknown names are an error that lists the
        valid ones; selection order is the config's order.
        """
        specs = self.host_specs
        if not names:
            return specs
        by_name = {s.name: s for s in specs}
        unknown = [n for n in names if n not in by_name]
        if unknown:
            raise ValueError(
                f"unknown host '{unknown[0]}' (config hosts: {', '.join(by_name)})")
        wanted = set(names)
        return [s for s in specs if s.name in wanted]   # config order, deduped

    def view_for(self, host_name: str) -> "Config":
        """The cluster-wide config with one host's overrides applied (v3).

        Deep-merges the host entry's override keys over the base data, then the
        host's per-model ``host_overrides`` over each model's own fields. The
        result is a full :class:`Config`, so render/converge/plan run against
        it without knowing a cluster exists. Legacy configs: the base itself.
        Views are cached per base config.
        """
        if not self.is_v3 or self._view_base is not None:
            return self
        if self._view_host == host_name:
            return self
        cached = getattr(self, "_views", None)
        if cached is None:
            cached = self._views = {}
        elif host_name in cached:
            return cached[host_name]
        spec = self.select_hosts([host_name])[0]
        merged = deep_merge(self.data, {k: v for k, v in spec.overrides.items() if k != "hosts"})
        merged.pop("hosts", None)   # host selection lives on the base config
        models = dict(merged.get("models") or {})
        serving_litellm: Dict[str, Any] = {}
        for alias, mdef in models.items():
            mdef = dict(mdef or {})
            per_host = mdef.pop("host_overrides", None) or {}
            ov = per_host.get(host_name)
            if ov:
                mdef = deep_merge(mdef, ov)
                # A model's per-host litellm block is its serving identity on
                # this host: it also applies to the gateway section itself.
                if isinstance(ov.get("litellm"), dict):
                    serving_litellm = deep_merge(serving_litellm, ov["litellm"])
            models[alias] = mdef
        if models:
            merged["models"] = models
        if serving_litellm:
            merged["litellm"] = deep_merge(merged.get("litellm") or {}, serving_litellm)
        view = Config(merged, self.config_path, self.env, _host=host_name, _base=self)
        cached[host_name] = view
        return view

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
        """The target node's spark-lab checkout (state + upgrade), raw form.

        Legacy accessor: the v3 form is :attr:`repo_dir` (per-host ``install.repo_dir``,
        falling back to the legacy ``install.remote.repo_dir``).
        """
        return str(self.remote.get("repo_dir") or "~/spark-lab")

    @property
    def repo_dir(self) -> str:
        """This config's node-side spark-lab checkout (state + upgrade), raw form.

        v3: the (per-host-overridable) ``install.repo_dir``; legacy: the
        ``install.remote.repo_dir``; default ``~/spark-lab``.
        """
        rd = (self.install or {}).get("repo_dir")
        if rd:
            return str(rd)
        return self.remote_repo_dir

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
