"""Unit tests for template rendering (deterministic, no node)."""

import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import config as config_mod, render  # noqa: E402
from tests.helpers import REFERENCE_CONFIG, REFERENCE_ENV, SECRET_DUMMY  # noqa: E402


def _render():
    d = Path(tempfile.mkdtemp())
    (d / "config.yaml").write_text(REFERENCE_CONFIG)
    (d / ".env").write_text(REFERENCE_ENV)
    cfg = config_mod.load(str(d / "config.yaml"))
    rendered = render.render(cfg, d / "deploy")
    return cfg, rendered


class TestRender(unittest.TestCase):
    def setUp(self):
        # Pin ambient secrets + sparkrun so renders are deterministic and a real
        # host token can never leak into output.
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_renders_all_eleven_targets(self):
        cfg, rendered = _render()
        self.assertIn(f"sparkrun/recipes/{cfg.recipe_name}.yaml", rendered)
        self.assertIn("litellm/docker-compose.yml", rendered)
        self.assertIn("litellm/config.yaml", rendered)
        self.assertIn("litellm/model_config.yaml", rendered)
        self.assertIn("litellm/.env", rendered)
        self.assertIn("litellm/prometheus.yml", rendered)
        self.assertEqual(len(rendered), 11)

    def test_recipe_target_embeds_model_and_flags(self):
        cfg, rendered = _render()
        text = rendered[f"sparkrun/recipes/{cfg.recipe_name}.yaml"].decode("utf-8")
        self.assertIn("test-llm/model", text)
        self.assertIn("lmsysorg/sglang:test", text)
        self.assertIn("--kv-cache-dtype", text)
        # the recipe name is in the target path, not the body
        self.assertEqual(f"sparkrun/recipes/{cfg.recipe_name}.yaml", "sparkrun/recipes/qwen.yaml")

    def test_recipe_uses_pinned_hf_token_not_ambient(self):
        _cfg, rendered = _render()
        text = rendered["sparkrun/recipes/qwen.yaml"].decode("utf-8")
        self.assertIn("test-hf-token", text)

    def test_verbatim_json_copied_byte_identically(self):
        _cfg, rendered = _render()
        tmpl = ROOT / "sparklab" / "templates" / "grafana" / "dashboards" / "sglang-dashboard.json"
        self.assertEqual(
            rendered["litellm/grafana/dashboards/sglang-dashboard.json"],
            tmpl.read_bytes(),
        )

    def test_secrets_resolved_into_litellm_env(self):
        _cfg, rendered = _render()
        env_text = rendered["litellm/.env"].decode("utf-8")
        self.assertIn("test-master-key", env_text)
        self.assertIn("test-salt-key", env_text)

    def test_model_config_allows_reasoning_effort(self):
        # Self-hosted engines (sglang/vllm) take reasoning controls as extra
        # params; without this allowlist the gateway 400s on reasoning_effort.
        _cfg, rendered = _render()
        text = rendered["litellm/model_config.yaml"].decode("utf-8")
        self.assertIn('allowed_openai_params: ["reasoning_effort"]', text)

    def test_missing_key_names_fall_back_to_default_env_names(self):
        # An omitted master_key_env/salt_key_env must resolve the standard .env
        # names -- never silently render empty secrets into litellm/.env.
        import re as _re
        d = Path(tempfile.mkdtemp())
        no_names = _re.sub(
            r"\n\s+(master_key_env|salt_key_env):.+", "",
            REFERENCE_CONFIG)
        self.assertNotIn("master_key_env", no_names)
        (d / "config.yaml").write_text(no_names)
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        rendered = render.render(cfg, d / "deploy")
        env_text = rendered["litellm/.env"].decode("utf-8")
        self.assertIn("test-master-key", env_text)
        self.assertIn("test-salt-key", env_text)

    def test_rendered_files_written_to_deploy_dir(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(REFERENCE_CONFIG)
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        render.render(cfg, d / "deploy")
        self.assertTrue((d / "deploy" / "litellm" / "config.yaml").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
