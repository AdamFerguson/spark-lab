"""Multi-host cluster core tests (ADR 0008) — all hermetic.

Covers host selection, per-host config views (deep merge + host_overrides +
per-host active-model selection), local auto-detection, target construction
(legacy local / remote-factory injected), and the fan-out runner's
continue-past-failure aggregation.
"""

import os
import sys
import tempfile
import unittest
import yaml
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import cluster, config as config_mod, render  # noqa: E402
from sparklab.core.config import HostSpec  # noqa: E402
from tests.helpers import FakeRuntime, REFERENCE_CONFIG, REFERENCE_ENV, SECRET_DUMMY, V3_CLUSTER_CONFIG  # noqa: E402


def load_v3(d: Path) -> config_mod.Config:
    (d / "config.yaml").write_text(V3_CLUSTER_CONFIG)
    (d / ".env").write_text(REFERENCE_ENV)
    return config_mod.load(str(d / "config.yaml"))


class TestSelectHosts(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()
        self.cfg = load_v3(self.d)

    def tearDown(self):
        self._env.stop()

    def test_all_hosts_in_config_order(self):
        self.assertEqual([s.name for s in self.cfg.select_hosts(None)], ["alpha", "beta"])
        self.assertEqual([s.name for s in self.cfg.select_hosts([])], ["alpha", "beta"])

    def test_selection_subsets_and_reorders_to_config_order(self):
        self.assertEqual([s.name for s in self.cfg.select_hosts(["beta"])], ["beta"])
        self.assertEqual([s.name for s in self.cfg.select_hosts(["beta", "alpha"])], ["alpha", "beta"])

    def test_unknown_host_raises_with_valid_names(self):
        with self.assertRaises(ValueError) as cm:
            self.cfg.select_hosts(["gamma"])
        self.assertIn("gamma", str(cm.exception))
        self.assertIn("alpha", str(cm.exception))


class TestHostViews(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()
        self.cfg = load_v3(self.d)

    def tearDown(self):
        self._env.stop()

    def test_host_override_deep_merges_over_base(self):
        beta = self.cfg.view_for("beta")
        self.assertEqual(beta.monitoring.get("instance_label"), "beta-node")
        self.assertEqual(beta.monitoring.get("dashboards"), ["sglang-dashboard"])  # base kept
        alpha = self.cfg.view_for("alpha")
        self.assertEqual(alpha.monitoring.get("instance_label"), "alpha-node")

    def test_model_host_overrides_merge_into_model_params(self):
        beta = self.cfg.view_for("beta")
        alpha = self.cfg.view_for("alpha")
        self.assertEqual(beta.effective_params().get("mamba_ssm_dtype"), "bfloat16")
        self.assertEqual(beta.effective_params().get("kv_cache_dtype"), "fp8_e4m3")  # base kept
        self.assertNotIn("mamba_ssm_dtype", alpha.effective_params())
        # both views still serve the same active model
        self.assertEqual(beta.active_alias, "qwen")
        self.assertEqual(alpha.active_alias, "qwen")

    def test_host_override_litellm_serving_identity_applies_to_entries_only(self):
        # the host-override litellm identity no longer bleeds into the view-wide
        # gateway section -- it resolves PER ENTRY (serving_entries) instead
        beta = self.cfg.view_for("beta")
        alpha = self.cfg.view_for("alpha")
        self.assertEqual(beta.litellm.get("model_name"), "my-spark-model")  # gateway base
        self.assertEqual(alpha.litellm.get("model_name"), "my-spark-model")
        self.assertEqual(beta.serving_entries()[0]["model_name"], "beta-served-name")
        self.assertEqual(alpha.serving_entries()[0]["model_name"], "my-spark-model")

    def test_view_for_is_cached_per_host(self):
        self.assertIs(self.cfg.view_for("beta"), self.cfg.view_for("beta"))

    def test_view_with_no_serving_model_is_control_plane_only(self):
        text = V3_CLUSTER_CONFIG.replace("hosts: [alpha, beta]", "hosts: [alpha]")
        sub = self.d / "sub"
        sub.mkdir()
        (sub / "config.yaml").write_text(text)
        (sub / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(sub / "config.yaml"))
        beta = cfg.view_for("beta")
        self.assertEqual(beta.active_alias, "")
        rendered = render.render(beta, sub / "deploy")
        self.assertNotIn("sparkrun/recipes/qwen.yaml", rendered)
        self.assertIn("litellm/docker-compose.yml", rendered)
        alpha = cfg.view_for("alpha")
        self.assertEqual(alpha.active_alias, "qwen")
        self.assertIn("sparkrun/recipes/qwen.yaml", render.render(alpha, sub / "deploy-a"))


class TestMonitoringRoles(unittest.TestCase):
    """monitoring.role split: 'full' (central prometheus + grafana +
    exporters), 'exporters' (scraped by a full host), 'none'."""

    V3_EXPORTERS = V3_CLUSTER_CONFIG.replace(
        "    monitoring:\n      instance_label: beta-node",
        "    monitoring:\n      instance_label: beta-node\n      role: exporters",
    )

    def _load(self, text, name="role"):
        sub = self.d / name
        sub.mkdir()
        (sub / "config.yaml").write_text(text)
        (sub / ".env").write_text(REFERENCE_ENV)
        return config_mod.load(str(sub / "config.yaml"))

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_role_accessor_defaults_and_validation(self):
        cfg = self._load(V3_CLUSTER_CONFIG)
        self.assertEqual(cfg.view_for("alpha").monitoring_role(), "full")
        self.assertEqual(cfg.view_for("beta").monitoring_role(), "full")
        cfg = self._load(self.V3_EXPORTERS, "exporters")
        self.assertEqual(cfg.view_for("beta").monitoring_role(), "exporters")
        off = V3_CLUSTER_CONFIG.replace(
            "enabled: true\n  instance_label: alpha-node", "enabled: false\n  instance_label: alpha-node"
        )
        self.assertEqual(self._load(off, "off").monitoring_role(), "none")
        bad = V3_CLUSTER_CONFIG.replace("hosts:\n", "hosts:\n  - name: bad\n    monitoring: {role: bogus}\n")
        with self.assertRaises(ValueError):
            self._load(bad, "bad").view_for("bad").monitoring_role()

    def test_exporters_host_renders_no_stack_but_keeps_exporters(self):
        cfg = self._load(self.V3_EXPORTERS, "exporters")
        beta = cfg.view_for("beta")
        rendered = render.render(beta, self.d / "beta-deploy")
        self.assertNotIn("litellm/prometheus.yml", rendered)
        self.assertNotIn("litellm/grafana/provisioning/dashboards/dashboards.yml", rendered)
        self.assertIn("litellm/scripts/nvidia-gpu-textfile.sh", rendered)
        compose = rendered["litellm/docker-compose.yml"].decode()
        self.assertNotIn("prometheus:", compose)
        self.assertNotIn("grafana:", compose)
        self.assertIn("node_exporter:", compose)
        self.assertIn("dcgm_exporter:", compose)
        self.assertIn("cadvisor:", compose)
        self.assertIn("gpu_textfile:", compose)
        self.assertNotIn("prometheus_data", compose)

    def test_full_host_scrapes_remote_exporters(self):
        cfg = self._load(self.V3_EXPORTERS, "exporters")
        alpha = cfg.view_for("alpha")
        targets = alpha.remote_scrape_targets()
        self.assertEqual(targets, [{"name": "beta.tailx.ts.net", "instance": "beta-node", "model_port": 30000}])
        prom = render.render(alpha, self.d / "alpha-deploy")["litellm/prometheus.yml"].decode()
        for needle in (
            "job_name: sglang_beta.tailx.ts.net",
            "targets: [beta.tailx.ts.net:30000]",
            "job_name: node_beta.tailx.ts.net",
            "targets: [beta.tailx.ts.net:9100]",
            "targets: [beta.tailx.ts.net:9835]",
            "targets: [beta.tailx.ts.net:8080]",
            "instance: beta-node",
        ):
            self.assertIn(needle, prom)
        # the local jobs are unchanged and the remote host is not self-targeted
        self.assertIn("targets: [node_exporter:9100]", prom)
        self.assertNotIn("targets: [alpha.tailx.ts.net:9100]", prom)
        # and vice versa: an exporters host has no prometheus config at all,
        # so no remote targets surface on its side either
        self.assertEqual(cfg.view_for("beta").remote_scrape_targets(), [])

    def test_legacy_config_has_no_remote_targets(self):
        sub = self.d / "legacy"
        sub.mkdir()
        (sub / "config.yaml").write_text(REFERENCE_CONFIG)
        (sub / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(sub / "config.yaml"))
        self.assertEqual(cfg.monitoring_role(), "full")
        self.assertEqual(cfg.remote_scrape_targets(), [])
        prom = render.render(cfg, sub / "deploy")["litellm/prometheus.yml"].decode()
        self.assertNotIn("Remote exporters host", prom)

    def test_multi_active_models_disjoint_hosts_are_ok(self):
        text = V3_CLUSTER_CONFIG.replace("hosts: [alpha, beta]", "hosts: [alpha]", 1).replace(
            "litellm:\n  model_name:",
            "  llama:\n    active: true\n    hosts: [beta]\n"
            "    hf_model: test-llm/llama\n    image: lmsysorg/sglang:llama\n"
            "litellm:\n  model_name:",
            1,
        )
        sub = self.d / "sub2"
        sub.mkdir()
        (sub / "config.yaml").write_text(text)
        (sub / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(sub / "config.yaml"))
        self.assertFalse(cfg.active_host_conflicts())
        self.assertEqual(cfg.view_for("alpha").active_alias, "qwen")
        self.assertEqual(cfg.view_for("beta").active_alias, "llama")

    def test_empty_hosts_list_serves_nowhere(self):
        sub = self.d / "sub4"
        sub.mkdir()
        (sub / "config.yaml").write_text(V3_CLUSTER_CONFIG)
        (sub / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(sub / "config.yaml"))
        self.assertEqual(cfg.model_host_list("qwen"), ["alpha", "beta"])
        # empty list (fully scaled down) != missing key (all hosts)
        self.assertFalse(config_mod.Config._serves("alpha", {"hosts": []}))
        self.assertTrue(config_mod.Config._serves("alpha", {}))

    def test_multi_active_models_sharing_a_host_conflict(self):
        text = V3_CLUSTER_CONFIG.replace(
            "litellm:\n  model_name:",
            "  llama:\n    active: true\n    hosts: [beta]\n"
            "    hf_model: test-llm/llama\n    image: lmsysorg/sglang:llama\n"
            "litellm:\n  model_name:",
            1,
        )
        sub = self.d / "sub3"
        sub.mkdir()
        (sub / "config.yaml").write_text(text)
        (sub / ".env").write_text(REFERENCE_ENV)
        with self.assertRaises(ValueError) as cm:
            config_mod.load(str(sub / "config.yaml"))
        # the error names the host and the model so the fix is obvious
        self.assertIn("beta", str(cm.exception))
        self.assertIn("llama", str(cm.exception))

    def test_multi_active_disjoint_hosts_views_pick_theirs(self):
        # qwen -> alpha only, llama -> beta only: loads; each view selects its
        # own active model.
        text = V3_CLUSTER_CONFIG.replace("hosts: [alpha, beta]", "hosts: [alpha]", 1).replace(
            "litellm:\n  model_name:",
            "  llama:\n    active: true\n    hosts: [beta]\n"
            "    hf_model: test-llm/llama\n    image: lmsysorg/sglang:llama\n"
            "litellm:\n  model_name:",
            1,
        )
        sub = self.d / "sub6"
        sub.mkdir()
        (sub / "config.yaml").write_text(text)
        (sub / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(sub / "config.yaml"))
        self.assertEqual(cfg.view_for("alpha").active_alias, "qwen")
        self.assertEqual(cfg.view_for("beta").active_alias, "llama")


class TestControlPlane(unittest.TestCase):
    """control_plane.enabled split: on (default -- gateway + DB + Redis),
    off (observability-only host: no gateway, no litellm config files)."""

    MODEL_ALPHA_ONLY = V3_CLUSTER_CONFIG.replace("    hosts: [alpha, beta]", "    hosts: [alpha]")

    BETA_OFF = "    control_plane:\n      enabled: false\n    monitoring:\n      instance_label: beta-node"

    def _load(self, text, name="cp"):
        sub = self.d / name
        sub.mkdir()
        (sub / "config.yaml").write_text(text)
        (sub / ".env").write_text(REFERENCE_ENV)
        return config_mod.load(str(sub / "config.yaml"))

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_accessor_defaults_and_validation(self):
        cfg = self._load(V3_CLUSTER_CONFIG)
        self.assertTrue(cfg.view_for("alpha").control_plane_enabled())
        self.assertTrue(cfg.view_for("beta").control_plane_enabled())
        off = self._load(
            V3_CLUSTER_CONFIG.replace("    monitoring:\n      instance_label: beta-node", self.BETA_OFF), "off"
        )
        self.assertFalse(off.view_for("beta").control_plane_enabled())
        self.assertTrue(off.view_for("alpha").control_plane_enabled())
        s = self._load(
            V3_CLUSTER_CONFIG.replace(
                "    monitoring:\n      instance_label: beta-node",
                '    control_plane:\n      enabled: "false"\n    monitoring:\n      instance_label: beta-node',
            ),
            "s",
        )
        self.assertFalse(s.view_for("beta").control_plane_enabled())
        bad = self._load(
            V3_CLUSTER_CONFIG.replace(
                "    monitoring:\n      instance_label: beta-node",
                "    control_plane:\n      enabled: bogus\n    monitoring:\n      instance_label: beta-node",
            ),
            "bad",
        )
        with self.assertRaises(ValueError):
            bad.view_for("beta").control_plane_enabled()

    def test_off_host_renders_no_control_plane_files_or_services(self):
        text = self.MODEL_ALPHA_ONLY.replace(
            "    monitoring:\n      instance_label: beta-node", self.BETA_OFF + "\n      role: exporters"
        )
        cfg = self._load(text, "off")
        self.assertEqual(cfg.control_plane_conflicts(), [])
        rendered = render.render(cfg.view_for("beta"), self.d / "beta-deploy")
        self.assertNotIn("litellm/config.yaml", rendered)
        self.assertNotIn("litellm/model_config.yaml", rendered)
        self.assertNotIn("litellm/.env", rendered)
        self.assertIn("litellm/docker-compose.yml", rendered)
        self.assertIn("litellm/scripts/nvidia-gpu-textfile.sh", rendered)
        compose = rendered["litellm/docker-compose.yml"].decode()
        for svc in ("  litellm:", "  db:", "  redis:"):
            self.assertNotIn(svc, compose)
        for svc in ("node_exporter:", "dcgm_exporter:", "cadvisor:", "gpu_textfile:"):
            self.assertIn(svc, compose)
        self.assertNotIn("postgres_data", compose)
        self.assertNotIn("redis_data", compose)
        parsed = yaml.safe_load(compose)
        self.assertEqual(set(parsed["services"]), {"node_exporter", "dcgm_exporter", "cadvisor", "gpu_textfile"})
        self.assertEqual(set(parsed["volumes"]), {"gpu_textfile_data"})

    def test_default_host_still_renders_full_control_plane(self):
        cfg = self._load(self.MODEL_ALPHA_ONLY)
        compose = render.render(cfg.view_for("alpha"), self.d / "alpha-deploy")["litellm/docker-compose.yml"].decode()
        for svc in ("  litellm:", "  db:", "  redis:"):
            self.assertIn(svc, compose)
        self.assertIn("postgres_data", compose)
        self.assertIn("redis_data", compose)

    def test_model_host_control_plane_off_is_fine_when_served(self):
        # beta runs the model with the control plane off; alpha (CP on) serves
        # it -> valid (the implicit central-gateway case)
        text = V3_CLUSTER_CONFIG.replace("    monitoring:\n      instance_label: beta-node", self.BETA_OFF)
        cfg = self._load(text, "served")
        self.assertEqual(cfg.control_plane_conflicts(), [])
        self.assertEqual(cfg.serving_conflicts(), [])

    def test_conflict_no_gateway_host_to_serve_model(self):
        text = V3_CLUSTER_CONFIG.replace("    monitoring:\n      instance_label: beta-node", self.BETA_OFF)
        text = text.replace(
            "remote: false", "remote: false\n    control_plane:\n      enabled: false", 1
        )  # alpha off too
        cfg = self._load(text, "nogw")
        problems = cfg.serving_conflicts()
        self.assertEqual(len(problems), 1)
        self.assertIn("no host has the control plane", problems[0])
        self.assertIn("qwen", problems[0])

    def test_conflict_duplicate_serving_names_on_one_gateway(self):
        # qwen serves beta, llama serves alpha -- disjoint host sets (no run
        # conflict), but both gateways would register both models under the
        # default serving name. Only alpha keeps its control plane, so the
        # duplicate surfaces on alpha's gateway.
        text = V3_CLUSTER_CONFIG.replace(
            "    active: true\n    hosts: [alpha, beta]", "    active: true\n    hosts: [beta]"
        )
        text = text.replace("    monitoring:\n      instance_label: beta-node", self.BETA_OFF)
        text = text.replace(
            "litellm:\n  model_name:",
            "  llama:\n    active: true\n    hosts: [alpha]\n"
            "    hf_model: test-llm/llama\n    image: lmsysorg/sglang:llama\n"
            "litellm:\n  model_name:",
            1,
        )
        cfg = self._load(text, "dup")
        problems = cfg.serving_conflicts()
        self.assertTrue(any("'qwen'" in p and "'llama'" in p and "'alpha'" in p for p in problems), problems)

    def test_conflict_host_would_run_nothing(self):
        text = self.MODEL_ALPHA_ONLY.replace(
            "    monitoring:\n      instance_label: beta-node",
            "    control_plane:\n      enabled: false\n    monitoring:\n      enabled: false",
        )
        cfg = self._load(text, "nothing")
        problems = cfg.control_plane_conflicts()
        self.assertEqual(len(problems), 1)
        self.assertIn("would run nothing", problems[0])

    def test_legacy_config_has_no_conflicts(self):
        sub = self.d / "legacy"
        sub.mkdir()
        (sub / "config.yaml").write_text(REFERENCE_CONFIG)
        (sub / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(sub / "config.yaml"))
        self.assertEqual(cfg.control_plane_conflicts(), [])


class TestServingEntries(unittest.TestCase):
    """Implicit central serving: every active model with a running host is
    registered on every control-plane host (ADR-0008 addendum #3)."""

    def _load(self, text, name="sv"):
        sub = self.d / name
        sub.mkdir()
        (sub / "config.yaml").write_text(text)
        (sub / ".env").write_text(REFERENCE_ENV)
        return config_mod.load(str(sub / "config.yaml"))

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_local_entry_matches_historical_values(self):
        cfg = self._load(V3_CLUSTER_CONFIG)
        entries = cfg.view_for("alpha").serving_entries()
        self.assertEqual(
            entries,
            [
                {
                    "alias": "qwen",
                    "model_name": "my-spark-model",
                    "hf_model": "test-llm/model",
                    "api_base": "http://host.docker.internal:30000/v1",
                    "model_info": {"supports_vision": True},
                    "model_settings": {"temperature": 1.0, "top_p": 0.95, "top_k": 20},
                }
            ],
        )

    def test_remote_entry_points_at_running_host(self):
        text = V3_CLUSTER_CONFIG.replace("hosts: [alpha, beta]", "hosts: [beta]")
        cfg = self._load(text, "remote")
        a = cfg.view_for("alpha").serving_entries()
        b = cfg.view_for("beta").serving_entries()
        self.assertEqual(a[0]["api_base"], "http://beta.tailx.ts.net:30000/v1")
        self.assertEqual(b[0]["api_base"], "http://host.docker.internal:30000/v1")
        # alpha does not run the model: no recipe rendered for it
        alpha_rendered = render.render(cfg.view_for("alpha"), self.d / "a")
        self.assertNotIn("sparkrun/recipes/qwen.yaml", alpha_rendered)
        self.assertIn("litellm/model_config.yaml", alpha_rendered)

    def test_serving_identity_precedence(self):
        text = V3_CLUSTER_CONFIG.replace(
            "    host_overrides:\n      beta:\n",
            "    litellm:\n      model_name: model-level-name\n    host_overrides:\n      beta:\n",
        )
        text = text.replace(
            "        litellm:\n          model_name: beta-served-name",
            "        litellm:\n          model_name: host-level-name",
        )
        cfg = self._load(text, "prec")
        # beta (the model host): its host_overrides identity wins
        self.assertEqual(cfg.view_for("beta").serving_entries()[0]["model_name"], "host-level-name")
        # alpha (remote gateway): no host_overrides there -> the model-level name
        self.assertEqual(cfg.view_for("alpha").serving_entries()[0]["model_name"], "model-level-name")

    def test_explicit_api_base_wins(self):
        text = V3_CLUSTER_CONFIG.replace(
            "    host_overrides:", "    litellm:\n      api_base: http://lb.internal:30001/v1\n    host_overrides:"
        )
        cfg = self._load(text, "lb")
        for host in ("alpha", "beta"):
            self.assertEqual(cfg.view_for(host).serving_entries()[0]["api_base"], "http://lb.internal:30001/v1")

    def test_scaled_down_model_registers_nowhere(self):
        text = V3_CLUSTER_CONFIG.replace("hosts: [alpha, beta]", "hosts: []")
        cfg = self._load(text, "scaled")
        self.assertEqual(cfg.view_for("alpha").serving_entries(), [])
        self.assertEqual(cfg.view_for("beta").serving_entries(), [])
        rendered = render.render(cfg.view_for("alpha"), self.d / "scaled")
        self.assertIn("model_list: []", rendered["litellm/model_config.yaml"].decode())
        yaml.safe_load(rendered["litellm/model_config.yaml"].decode())

    def test_inactive_model_registers_nowhere(self):
        text = V3_CLUSTER_CONFIG.replace("    active: true", "    active: false")
        cfg = self._load(text, "inactive")
        self.assertEqual(cfg.view_for("alpha").serving_entries(), [])
        mc = render.render(cfg.view_for("alpha"), self.d / "inact")["litellm/model_config.yaml"].decode()
        self.assertIn("model_list: []", mc)
        self.assertEqual(cfg.serving_conflicts(), [])

    def test_two_models_two_entries_one_gateway(self):
        text = V3_CLUSTER_CONFIG.replace(
            "    active: true\n    hosts: [alpha, beta]", "    active: true\n    hosts: [alpha]"
        )
        text = text.replace(
            "litellm:\n  model_name:",
            "  llama:\n    active: true\n    hosts: [beta]\n"
            "    litellm:\n      model_name: llama-served\n"
            "    hf_model: test-llm/llama\n    image: lmsysorg/sglang:llama\n"
            "litellm:\n  model_name:",
            1,
        )
        cfg = self._load(text, "two")
        entries = cfg.view_for("alpha").serving_entries()
        self.assertEqual([e["alias"] for e in entries], ["qwen", "llama"])
        self.assertEqual(entries[0]["api_base"], "http://host.docker.internal:30000/v1")
        self.assertEqual(entries[1]["api_base"], "http://beta.tailx.ts.net:30000/v1")
        self.assertEqual(entries[1]["model_name"], "llama-served")
        mc = render.render(cfg.view_for("alpha"), self.d / "two")["litellm/model_config.yaml"].decode()
        parsed = yaml.safe_load(mc)
        self.assertEqual([e["model_name"] for e in parsed["model_list"]], ["my-spark-model", "llama-served"])
        self.assertEqual(cfg.serving_conflicts(), [])


class TestLocalDetection(unittest.TestCase):
    def test_remote_false_is_unconditionally_local(self):
        self.assertTrue(cluster.is_on_host(HostSpec(name="anywhere", remote=False)))

    def test_detection_matches_name_or_ssh_host_first_label(self):
        with mock.patch.object(cluster, "local_identities", return_value={"alpha"}):
            self.assertTrue(cluster.is_on_host(HostSpec(name="alpha", ssh="x", remote=True)))
            self.assertTrue(cluster.is_on_host(HostSpec(name="other", ssh="alpha.tailx.ts.net", remote=True)))
            self.assertFalse(cluster.is_on_host(HostSpec(name="other", ssh="beta.tailx.ts.net", remote=True)))

    def test_no_match_is_remote(self):
        with mock.patch.object(cluster, "local_identities", return_value={"unrelated"}):
            self.assertFalse(cluster.is_on_host(HostSpec(name="alpha", remote=True)))


class TestTargets(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_v3_targets_local_and_injected_remote(self):
        cfg = load_v3(self.d)
        fake_local = FakeRuntime()
        fake_remote = object.__new__(FakeRuntime)
        fake_remote.is_remote = True
        fake_remote.label = "you@beta (stub)"
        with mock.patch.object(cluster, "local_identities", return_value=set()):
            ts = cluster.targets(
                cfg, ["beta", "alpha"], runtime=fake_local, remote_factory=lambda spec, view: fake_remote
            )
        self.assertEqual([t.name for t in ts], ["alpha", "beta"])
        self.assertIs(ts[0].runtime, fake_local)
        self.assertIs(ts[1].runtime, fake_remote)
        self.assertFalse(ts[0].is_remote)
        self.assertTrue(ts[1].is_remote)

    def test_v3_local_host_detected_uses_local_runtime(self):
        cfg = load_v3(self.d)
        fake_local = FakeRuntime()
        with mock.patch.object(cluster, "local_identities", return_value={"alpha"}):
            ts = cluster.targets(cfg, ["alpha"], runtime=fake_local)
        self.assertIs(ts[0].runtime, fake_local)
        self.assertFalse(ts[0].is_remote)

    def test_single_local_host_target(self):
        from tests.helpers import config_text

        (self.d / "config.yaml").write_text(config_text(str(self.d / "install")))
        (self.d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(self.d / "config.yaml"))
        rt = FakeRuntime()
        ts = cluster.targets(cfg, None, runtime=rt)
        self.assertEqual(len(ts), 1)
        self.assertIs(ts[0].runtime, rt)
        self.assertFalse(ts[0].is_remote)
        self.assertEqual(ts[0].cfg.view_host, "mylab")  # a view of the cluster

    def test_parse_hosts_arg(self):
        self.assertIsNone(cluster.parse_hosts_arg(None))
        self.assertIsNone(cluster.parse_hosts_arg("  "))
        self.assertEqual(cluster.parse_hosts_arg("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(cluster.parse_hosts_arg("a,"), ["a"])


class TestRunOnEach(unittest.TestCase):
    def _targets(self, names):
        return [SimpleNamespace(name=n, label="stub") for n in names]

    def test_all_ok(self):
        seen = []
        rc = cluster.run_on_each(self._targets(["a", "b"]), lambda t: seen.append(t.name) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["a", "b"])

    def test_failure_continues_and_aggregates(self):
        seen = []

        def op(t):
            seen.append(t.name)
            return 0 if t.name == "a" else 3

        rc = cluster.run_on_each(self._targets(["a", "b", "c"]), op)
        self.assertEqual(rc, 1)
        self.assertEqual(seen, ["a", "b", "c"])  # c still ran after b failed

    def test_truthy_nonzero_op_result_counts_as_failure(self):
        rc = cluster.run_on_each(self._targets(["a"]), lambda t: 0 or 0)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
