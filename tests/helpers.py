"""Shared test helpers: a deterministic reference config + a fake runtime.

The reference config is a *fixed* v1 document (no `version:` key) used by the
golden/regression tests. Its ``install_dir`` is a hard-coded absolute path
(``/opt/sparklab``) -- no ``~`` -- so the apply command argv the plan produces
is identical on every machine (the golden must be portable).

The reference ``.env`` uses obviously-fake values (no ``sk-`` / token-shaped
strings) so committing rendered output can never trip the secret scanner.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# Dummy values for every secret the engine can resolve. Tests pin the ambient
# environment to these (mock.patch.dict) so a real HF_TOKEN / master key / ...
# from the host can never leak into rendered output or the committed goldens.
# `Config.secret()` falls back to os.environ when the .env value is empty, so
# both the .env AND the ambient env are controlled in tests.
SECRET_DUMMY = {
    "LITELLM_MASTER_KEY": "test-master-key",
    "LITELLM_SALT_KEY": "test-salt-key",
    "LITELLM_DB_PASSWORD": "testdb",
    "GRAFANA_ADMIN_PASSWORD": "testgraf",
    "HF_TOKEN": "test-hf-token",
    "CF_TUNNEL_TOKEN": "test-cf-token",
}

# A minimal-but-complete v1 config: every template's context key is present so
# rendering is deterministic. install_dir is a fixed absolute path (portable).
REFERENCE_CONFIG = """\
install:
  name: mylab
  install_dir: /opt/sparklab
  hosts:
    - 127.0.0.1
model:
  recipe_name: qwen
  hf_model: test-llm/model
  image: lmsysorg/sglang:test
  hf_token_env: HF_TOKEN
  host: 0.0.0.0
  port: 30000
  min_nodes: 1
  params:
    kv_cache_dtype: fp8_e4m3
    mem_fraction_static: 0.85
    attention_backend: flashinfer
  extra_flags:
    - --enable-metrics
    - --trust-remote-code
litellm:
  model_name: my-spark-model
  port: 4000
  model_api_base_host: host.docker.internal
  master_key_env: LITELLM_MASTER_KEY
  salt_key_env: LITELLM_SALT_KEY
  db:
    image: pgvector/pgvector:pg16
    user: litellm
    password_env: LITELLM_DB_PASSWORD
    db: litellm
  redis:
    enabled: true
    image: redis:7-alpine
    port: 6379
  model_info:
    supports_vision: true
    supports_reasoning: true
monitoring:
  enabled: true
  prometheus:
    image: prom/prometheus
    port: 9090
    retention: 15d
  grafana:
    image: grafana/grafana
    port: 3000
    admin_password_env: GRAFANA_ADMIN_PASSWORD
  instance_label: spark
  dashboards:
    - sglang-dashboard
network:
  tailscale:
    enabled: true
  cloudflare:
    enabled: false
"""

REFERENCE_ENV = """\
LITELLM_MASTER_KEY=test-master-key
LITELLM_SALT_KEY=test-salt-key
LITELLM_DB_PASSWORD=testdb
GRAFANA_ADMIN_PASSWORD=testgraf
HF_TOKEN=test-hf-token
CF_TUNNEL_TOKEN=test-cf-token
"""

# A fixed v3 cluster config: two hosts (alpha = local-only, beta = remote with
# per-host monitoring override) sharing one model (beta gets a params override
# via host_overrides). install_dir is a fixed absolute path (portable goldens)
# and every secret resolves to the REFERENCE_ENV dummy values.
V3_CLUSTER_CONFIG = """\
version: 3
install:
  name: v3lab
  install_dir: /opt/sparklab
hosts:
  - name: alpha
    remote: false
  - name: beta
    ssh: beta.tailx.ts.net
    remote: true
    monitoring:
      instance_label: beta-node
models:
  qwen:
    active: true
    hosts: [alpha, beta]
    runtime: sglang
    hf_model: test-llm/model
    image: lmsysorg/sglang:test
    hf_token_env: HF_TOKEN
    host: 0.0.0.0
    port: 30000
    min_nodes: 1
    params:
      kv_cache_dtype: fp8_e4m3
      attention_backend: flashinfer
    extra_flags:
      - --enable-metrics
    host_overrides:
      beta:
        params:
          mamba_ssm_dtype: bfloat16
        litellm:
          model_name: beta-served-name
litellm:
  model_name: my-spark-model
  port: 4000
  model_api_base_host: host.docker.internal
  master_key_env: LITELLM_MASTER_KEY
  salt_key_env: LITELLM_SALT_KEY
  db:
    image: pgvector/pgvector:pg16
    user: litellm
    password_env: LITELLM_DB_PASSWORD
    db: litellm
  redis:
    enabled: true
    image: redis:7-alpine
    port: 6379
  model_info:
    supports_vision: true
monitoring:
  enabled: true
  instance_label: alpha-node
  prometheus:
    image: prom/prometheus
    port: 9090
    retention: 15d
  grafana:
    image: grafana/grafana
    port: 3000
    admin_password_env: GRAFANA_ADMIN_PASSWORD
  dashboards:
    - sglang-dashboard
network:
  tailscale:
    enabled: true
  cloudflare:
    enabled: false
"""


def config_text(install_dir: str, tailscale_enabled: bool = False) -> str:
    """The reference config with a given (writable) install_dir substituted in.

    Used by the integration tests so ``write_files`` lands in a temp dir instead
    of the fixed golden path. Tailscale is off by default so a converged
    re-apply issues zero commands (a clean no-op).
    """
    text = REFERENCE_CONFIG.replace("/opt/sparklab", str(install_dir))
    if not tailscale_enabled:
        text = text.replace(
            "network:\n  tailscale:\n    enabled: true",
            "network:\n  tailscale:\n    enabled: false",
        )
    return text


class FakeRuntime:
    """Records the commands that *would* run; returns canned exit codes.

    Mirrors ``sparklab.core.runtime.Runtime`` (``available`` + ``run`` +
    ``locate`` + ``home_path``) so it can stand in for the real runtime in any
    command handler. It is a *local* fake: node-side paths resolve against this
    machine (``home_path()`` is None).
    """

    is_remote = False

    def __init__(self, available=None, fail=None):
        self._available = set(available) if available is not None else {
            "sparkrun", "docker", "systemctl", "tailscale", "cloudflared",
        }
        self._fail = dict(fail or {})
        self.calls = []
        self.spawned = []   # the subset of commands launched detached (via spawn)
        self.spawn_logs = []   # the log path each detached launch was given

    def available(self, binary: str) -> bool:
        return binary in self._available

    def locate(self, binary: str):
        """Local fake: no node-side resolution (None -> caller's own fallback)."""
        return None

    def home_path(self):
        return None

    def run(self, argv):
        argv = [str(x) for x in argv]
        self.calls.append(argv)

        class _R:
            pass

        r = _R()
        r.argv = argv
        r.returncode = self._fail.get(argv[0], 0)
        return r

    def run_sudo(self, argv):
        """Mirror of ``Runtime.run_sudo``: records ``["sudo", *argv]``."""
        return self.run(["sudo"] + list(argv))

    def spawn(self, argv, log=None):
        """Record a detached launch (mirrors ``Runtime.spawn``).

        Recorded to the same ``calls`` list as ``run`` so command-sequence
        assertions stay uniform; returns a stand-in ``Popen`` (not awaited).
        ``log`` is the node-side launch-log path (recorded for assertions).
        """
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        self.spawned.append(argv)
        self.spawn_logs.append(log)

        class _P:
            pass

        p = _P()
        p.argv = argv
        return p

    @property
    def commands(self):
        return [list(c) for c in self.calls]

    def descriptions_containing(self, needle):
        return [a for a in self.calls if any(needle in str(x) for x in a)]
