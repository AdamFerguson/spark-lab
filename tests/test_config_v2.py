"""Schema v2 tests: multi-model selection, image precedence, params precedence,
and the v1 -> v2 upgrade (which must render byte-identically -- the R3 rule)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import config as config_mod, render, schema  # noqa: E402
from tests.helpers import REFERENCE_CONFIG, REFERENCE_ENV, SECRET_DUMMY  # noqa: E402


def _cfg(data, env=None):
    return config_mod.Config(data or {}, Path("/tmp/cfg/config.yaml"), env or {})


class TestSchemaV2(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    # -- version + active-model selection ---------------------------------
    def test_v1_is_not_v2_and_selects_single_model(self):
        c = _cfg({"model": {"recipe_name": "q", "hf_model": "x"}})
        self.assertFalse(c.is_v2)
        self.assertEqual(c.active_alias, "q")
        self.assertEqual(c.recipe_name, "q")
        self.assertEqual(c.model.get("hf_model"), "x")

    def test_v2_single_active(self):
        c = _cfg({"version": 2, "models": {
            "a": {"active": False, "hf_model": "a"},
            "b": {"active": True, "hf_model": "b"}}})
        self.assertTrue(c.is_v2)
        self.assertEqual(c.active_alias, "b")
        self.assertEqual(c.recipe_name, "b")
        self.assertEqual(list(c.models), ["a", "b"])

    def test_v2_active_models_wins_over_flags(self):
        c = _cfg({"version": 2, "models": {
            "a": {"active": True, "hf_model": "a"},
            "b": {"active": False, "hf_model": "b"}}, "active_models": ["b"]})
        self.assertEqual(c.active_alias, "b")

    def test_v2_two_active_raises(self):
        with self.assertRaises(ValueError):
            _cfg({"version": 2, "models": {"a": {"active": True}, "b": {"active": True}}})

    def test_v2_no_active_raises(self):
        with self.assertRaises(ValueError):
            _cfg({"version": 2, "models": {"a": {"active": False}, "b": {}}})

    def test_v2_bad_active_models_raises(self):
        with self.assertRaises(ValueError):
            _cfg({"version": 2, "models": {"a": {"active": True}},
                  "active_models": ["zzz"]})

    # -- image precedence --------------------------------------------------
    def test_image_precedence_env_profile_map_default(self):
        data = {"version": 2, "models": {"m": {"active": True, "image": "mm:1"}},
                "images": {"grafana": "map:1"},
                "profile": "dev", "profiles": {"dev": {"images": {"grafana": "prof:1"}}}}
        c = _cfg(data)
        self.assertEqual(c.image("grafana"), "prof:1")          # profile beats map
        with mock.patch.dict(os.environ, {"SPARKLAB_IMAGE_GRAFANA": "env:1"}):
            self.assertEqual(c.image("grafana"), "env:1")       # env beats profile
        self.assertEqual(c.image("redis"), "redis:7-alpine")    # falls to default

    def test_v1_image_field_fallback(self):
        c = _cfg({"model": {"recipe_name": "m", "image": "mm:1"},
                  "litellm": {"image": "lit:1"}})
        self.assertEqual(c.image("litellm"), "lit:1")
        self.assertEqual(c.image_model(), "mm:1")

    # -- params precedence -------------------------------------------------
    def test_effective_params_resources_wins(self):
        c = _cfg({"version": 2, "models": {"m": {"active": True,
                 "params": {"mem_fraction_static": 0.5, "kv": "fp8"},
                 "resources": {"mem_fraction_static": 0.9}}}})
        p = c.effective_params()
        self.assertEqual(p["mem_fraction_static"], 0.9)
        self.assertEqual(p["kv"], "fp8")

    def test_effective_params_defaults_to_v1_params(self):
        c = _cfg({"model": {"recipe_name": "m", "params": {"mem_fraction_static": 0.7}}})
        self.assertEqual(c.effective_params()["mem_fraction_static"], 0.7)

    # -- resolved image set ------------------------------------------------
    def test_resolved_images_reflect_enabled_stack(self):
        c = _cfg({"version": 2, "models": {"m": {"active": True, "image": "mm:1"}},
                  "litellm": {"redis": {"enabled": False}}})
        ri = c.resolved_images()
        self.assertIn("model", ri)
        self.assertNotIn("redis", ri)        # redis disabled
        self.assertIn("grafana", ri)         # monitoring on by default

        c2 = _cfg({"version": 2, "models": {"m": {"active": True, "image": "mm:1"}},
                   "monitoring": {"enabled": False}})
        self.assertNotIn("grafana", c2.resolved_images())

    # -- v1 -> v2 upgrade --------------------------------------------------
    def test_upgrade_to_v2_renders_byte_identical(self):
        v1data = yaml.safe_load(REFERENCE_CONFIG)
        d1 = Path(tempfile.mkdtemp())
        (d1 / "config.yaml").write_text(REFERENCE_CONFIG)
        (d1 / ".env").write_text(REFERENCE_ENV)
        r1 = render.render(config_mod.load(str(d1 / "config.yaml")), Path(tempfile.mkdtemp()))

        v2data = schema.upgrade_to_v2(v1data)
        d2 = Path(tempfile.mkdtemp())
        (d2 / "config.yaml").write_text(yaml.safe_dump(v2data, sort_keys=False))
        (d2 / ".env").write_text(REFERENCE_ENV)
        r2 = render.render(config_mod.load(str(d2 / "config.yaml")), Path(tempfile.mkdtemp()))

        self.assertEqual(r1, r2)  # byte-identical (R3)

    def test_upgrade_to_v2_preserves_values(self):
        v1data = yaml.safe_load(REFERENCE_CONFIG)
        v2 = schema.upgrade_to_v2(v1data)
        alias = str(v1data["model"].get("recipe_name") or "model")
        self.assertEqual(v2["version"], 2)
        self.assertNotIn("model", v2)
        self.assertIn(alias, v2["models"])
        self.assertTrue(v2["models"][alias]["active"])
        self.assertEqual(v2["models"][alias]["hf_model"], v1data["model"]["hf_model"])
        self.assertEqual(v2["models"][alias]["params"], v1data["model"].get("params", {}))
        self.assertEqual(v2["images"]["db"], "pgvector/pgvector:pg16")


if __name__ == "__main__":
    unittest.main(verbosity=2)
