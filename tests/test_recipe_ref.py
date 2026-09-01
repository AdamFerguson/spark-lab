"""Recipe reference (v3 placement-only model blocks) tests -- all hermetic.

Covers ADR-0009: `models.<m>.recipe:` resolves to `<config_dir>/recipes/
<m>.yaml` (a plain, directly-runnable sparkrun recipe), inline placement
keys win, sparkrun-native validation, layout pins, and the derived
placement table.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import config as config_mod, recipes as recipe_mod, render  # noqa: E402
from sparklab.core.recipes import RecipeError  # noqa: E402

RECIPE_YAML = """\
name: test-model
model: test-llm/alpha
model_revision: abc123
runtime: sglang
min_nodes: 1
container: lmsysorg/sglang:test
executor: docker
executor_config:
  ipc: host
  shm_size: 32g
  privileged: true
  cap_add:
    - SYS_PTRACE
  security_opt:
    - seccomp=unconfined
metadata:
  description: "a test model"
  model_dtype: nvfp4
  readiness_seconds: 1234
  litellm:
    model_name: test-gateway-name
    model_info:
      supports_reasoning: true
defaults:
  host: 0.0.0.0
  port: 30000
  kv_cache_dtype: fp8_e4m3
  max_running_requests: 4
command: |
  sglang serve \\
    --model-path {model} \\
    --kv-cache-dtype {kv_cache_dtype} \\
    --host {host} \\
    --port {port}
"""

CONFIG_YAML = """\
version: 3
install:
  name: reflab
  install_dir: ~/AI
hosts:
  - name: alpha
    remote: false
  - name: beta
    ssh: beta.tailx.ts.net
    remote: true
models:
  my-model:
    active: true
    hosts: [alpha]
    recipe: test-model
    hf_token_env: HF_TOKEN
"""


def make_dir(d: Path, config_text: str = CONFIG_YAML,
             recipes: dict | None = None) -> Path:
    (d / "recipes").mkdir(parents=True, exist_ok=True)
    for name, text in (recipes if recipes is not None else {"test-model": RECIPE_YAML}).items():
        (d / "recipes" / f"{name}.yaml").write_text(text)
    (d / "config.yaml").write_text(config_text)
    (d / ".env").write_text("HF_TOKEN=test-hf-token\n")
    return d


def load(d: Path) -> config_mod.Config:
    return config_mod.load(str(d / "config.yaml"))


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_reference_maps_recipe_fields_into_model_block(self):
        make_dir(self.d)
        m = load(self.d).model
        self.assertEqual(m["hf_model"], "test-llm/alpha")
        self.assertEqual(m["image"], "lmsysorg/sglang:test")
        self.assertEqual(m["runtime"], "sglang")
        self.assertEqual(m["min_nodes"], 1)
        self.assertEqual(m["model_revision"], "abc123")
        self.assertEqual(m["host"], "0.0.0.0")
        self.assertEqual(m["port"], 30000)
        self.assertEqual(m["params"],
                         {"kv_cache_dtype": "fp8_e4m3", "max_running_requests": 4})
        self.assertIn("--model-path {model}", m["serve_command"])
        self.assertEqual(m["executor_config"]["shm_size"], "32g")
        self.assertEqual(m["readiness_seconds"], 1234)
        self.assertEqual(m["litellm"]["model_name"], "test-gateway-name")
        self.assertEqual(m["metadata"]["model_dtype"], "nvfp4")
        # placement keys survive on the block
        self.assertEqual(m["hosts"], ["alpha"])
        self.assertEqual(m["recipe"], "test-model")
        self.assertTrue(m["active"])

    def test_inline_keys_win_over_recipe(self):
        make_dir(self.d, config_text=CONFIG_YAML.replace(
            "    recipe: test-model",
            "    recipe: test-model\n    port: 40000\n    params:\n      custom_flag: 7"))
        m = load(self.d).model
        self.assertEqual(m["port"], 40000)          # inline beats recipe defaults
        self.assertEqual(m["params"], {"custom_flag": 7})  # inline replaces
        self.assertEqual(m["hf_model"], "test-llm/alpha")  # recipe fills the rest

    def test_block_without_recipe_passes_through(self):
        text = CONFIG_YAML.replace("    recipe: test-model\n", "").replace(
            "    hf_token_env: HF_TOKEN",
            "    hf_model: test-llm/alpha\n    runtime: sglang")
        make_dir(self.d, config_text=text, recipes={})
        m = load(self.d).model
        self.assertEqual(m["hf_model"], "test-llm/alpha")
        self.assertNotIn("recipe", m)

    def test_inline_model_without_recipe_key(self):
        # A v3 model block may carry the launch spec inline (no recipe file).
        text = """\
version: 3
hosts:
  - name: alpha
models:
  m:
    active: true
    hf_model: test-llm/alpha
"""
        make_dir(self.d, config_text=text)
        m = load(self.d).model
        self.assertEqual(m["hf_model"], "test-llm/alpha")
        self.assertNotIn("recipe", m)

    def test_parked_model_reference_still_resolves(self):
        text = CONFIG_YAML.replace("hosts: [alpha]", "hosts: []")
        make_dir(self.d, config_text=text, recipes={})   # no recipe file
        with self.assertRaises(RecipeError):
            load(self.d)

    def test_missing_recipe_file_names_available(self):
        make_dir(self.d, config_text=CONFIG_YAML.replace(
            "recipe: test-model", "recipe: nope"))
        with self.assertRaises(RecipeError) as cm:
            load(self.d)
        self.assertIn("nope.yaml", str(cm.exception))
        self.assertIn("test-model", str(cm.exception))

    def test_name_stem_mismatch(self):
        make_dir(self.d, recipes={"test-model": RECIPE_YAML.replace(
            "name: test-model", "name: other-name")})
        with self.assertRaises(RecipeError) as cm:
            load(self.d)
        self.assertIn("other-name", str(cm.exception))

    def test_unknown_top_level_key_rejected(self):
        make_dir(self.d, recipes={"test-model": RECIPE_YAML +
                                  "sparklab:\n  litellm: {}\n"})
        with self.assertRaises(RecipeError) as cm:
            load(self.d)
        self.assertIn("sparklab", str(cm.exception))
        self.assertIn("metadata", str(cm.exception))

    def test_missing_model_rejected(self):
        make_dir(self.d, recipes={"test-model": RECIPE_YAML.replace(
            "model: test-llm/alpha\n", "")})
        with self.assertRaises(RecipeError):
            load(self.d)

    def test_missing_command_and_defaults_rejected(self):
        text = RECIPE_YAML.split("defaults:")[0]
        make_dir(self.d, recipes={"test-model": text})
        with self.assertRaises(RecipeError):
            load(self.d)


class TestHostOverridesAndGateway(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_host_overrides_beat_recipe_params(self):
        make_dir(self.d, config_text=CONFIG_YAML.replace(
            "    hf_token_env: HF_TOKEN",
            "    host_overrides:\n      alpha:\n        params:\n"
            "          kv_cache_dtype: bfloat16"))
        view = load(self.d).view_for("alpha")
        self.assertEqual(view.model["params"]["kv_cache_dtype"], "bfloat16")
        self.assertEqual(view.model["params"]["max_running_requests"], 4)

    def test_serving_entries_use_recipe_litellm_metadata(self):
        make_dir(self.d)
        base = load(self.d)
        local = base.view_for("alpha").serving_entries()
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0]["model_name"], "test-gateway-name")
        self.assertEqual(local[0]["model_info"]["supports_reasoning"], True)
        self.assertEqual(local[0]["api_base"], "http://host.docker.internal:30000/v1")
        # beta's gateway registers the SAME model at alpha's remote address
        # (alpha has no ssh: name is the address, per HostSpec.ssh_host)
        remote = base.view_for("beta").serving_entries()
        self.assertEqual(remote[0]["model_name"], "test-gateway-name")
        self.assertEqual(remote[0]["api_base"], "http://alpha:30000/v1")


class TestLayoutPins(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_single_host_pins_pool_default(self):
        make_dir(self.d)
        view = load(self.d).view_for("alpha")
        self.assertEqual(recipe_mod.layout_for_view(view), [("127.0.0.1", [0])])

    def test_spanning_pins_each_host_in_config_order(self):
        make_dir(self.d,
                 config_text=CONFIG_YAML.replace(
                     "  - name: alpha\n    remote: false",
                     "  - name: 10.0.0.1\n    remote: true").replace(
                     "hosts: [alpha]", "hosts: [10.0.0.1, 10.0.0.2]").replace(
                     "install_dir: ~/AI", "install_dir: ~/AI\n  hosts: [10.0.0.1, 10.0.0.2]"),
                 recipes={"test-model": RECIPE_YAML.replace(
                     "min_nodes: 1", "min_nodes: 2")})
        view = load(self.d).view_for("10.0.0.1")
        self.assertEqual(recipe_mod.layout_for_view(view),
                         [("10.0.0.1", [0]), ("10.0.0.2", [1])])

    def test_spanning_placement_is_run_pool_no_install_hosts(self):
        # A spanning model's run pool IS its own placement (models.<m>.hosts), so
        # it pins each placement host in config order without a global
        # install.hosts pool. (The old design required the placement hosts to be
        # a subset of install.hosts; that coupling is gone.)
        make_dir(self.d,
                 config_text=CONFIG_YAML.replace(
                     "hosts: [alpha]", "hosts: [alpha, beta]"),
                 recipes={"test-model": RECIPE_YAML.replace(
                     "min_nodes: 1", "min_nodes: 2")})
        view = load(self.d).view_for("alpha")
        self.assertEqual(recipe_mod.layout_for_view(view),
                         [("alpha", [0]), ("beta", [1])])
        rendered = render.render(view, self.d / "deploy")
        text = next(v for k, v in rendered.items()
                    if k.startswith("sparkrun/recipes/")).decode()
        self.assertIn("- host: alpha", text)
        self.assertIn("- host: beta", text)

    def test_layout_uses_host_ip_when_set(self):
        # sparkrun resolves layout pins / --hosts against cluster host IPs, not
        # hostnames: a host entry with an explicit `ip:` is pinned by that IP.
        make_dir(self.d,
                 config_text=CONFIG_YAML.replace(
                     "  - name: alpha\n    remote: false",
                     "  - name: alpha\n    remote: false\n    ip: 10.0.9.1").replace(
                     "  - name: beta\n    ssh: beta.tailx.ts.net\n    remote: true",
                     "  - name: beta\n    ssh: beta.tailx.ts.net\n    remote: true\n"
                     "    ip: 10.0.9.2").replace(
                     "hosts: [alpha]", "hosts: [alpha, beta]"),
                 recipes={"test-model": RECIPE_YAML.replace(
                     "min_nodes: 1", "min_nodes: 2")})
        view = load(self.d).view_for("alpha")
        self.assertEqual(recipe_mod.layout_for_view(view),
                         [("10.0.9.1", [0]), ("10.0.9.2", [1])])
        rendered = render.render(view, self.d / "deploy")
        text = next(v for k, v in rendered.items()
                    if k.startswith("sparkrun/recipes/")).decode()
        self.assertIn("- host: 10.0.9.1", text)
        self.assertIn("- host: 10.0.9.2", text)
        # placement stays NAME-based where it is compared against the config
        self.assertEqual(view.model_host_list("my-model"), ["alpha", "beta"])

    def test_json_string_default_renders_as_yaml_string(self):
        # A string default that YAML would otherwise parse as a mapping (the
        # JSON speculative_config) must round-trip as a STRING, or sparkrun
        # hands the runtime a str(dict) instead of the JSON.
        import json as _json
        import yaml as _yaml
        rec = {
            "name": "test-model", "model": "test-llm/alpha",
            "model_revision": "abc123", "runtime": "sglang", "min_nodes": 1,
            "container": "lmsysorg/sglang:test", "executor": "docker",
            "defaults": {"host": "0.0.0.0", "port": 8888,
                         "speculative_config":
                         '{"method":"dflash","model":"incoai/x",'
                         '"num_speculative_tokens":7}'},
        }
        text = _yaml.safe_dump(rec, sort_keys=False).rstrip() + \
            "\ncommand: |\n  vllm serve {model} '\\n    --speculative-config '\n"
        make_dir(self.d, recipes={"test-model": text})
        rendered = render.render(load(self.d).view_for("alpha"),
                                 self.d / "deploy")
        out = next(v for k, v in rendered.items()
                   if k.startswith("sparkrun/recipes/")).decode()
        doc = _yaml.safe_load(out)
        self.assertIsInstance(doc["defaults"]["speculative_config"], str)
        self.assertEqual(_json.loads(doc["defaults"]["speculative_config"])["model"],
                         "incoai/x")

    def test_span_fewer_hosts_than_min_nodes_fails_at_load(self):
        make_dir(self.d, config_text=CONFIG_YAML,
                 recipes={"test-model": RECIPE_YAML.replace(
                     "min_nodes: 1", "min_nodes: 2")})
        with self.assertRaises(ValueError) as cm:
            load(self.d)
        self.assertIn("spans 2 hosts", str(cm.exception))

    def test_span_check_skips_inactive_models(self):
        # A parked (active: false) spanning model has no placement plan:
        # hosts: [] must load fine (it is enabled by adding hosts later).
        make_dir(self.d, config_text=CONFIG_YAML.replace(
                     "    recipe: test-model",
                     "    active: false\n    hosts: []\n    recipe: test-model"),
                 recipes={"test-model": RECIPE_YAML.replace(
                     "min_nodes: 1", "min_nodes: 2")})
        cfg = load(self.d)  # must not raise
        # no host serves the parked model
        self.assertTrue(all(r[1] == "" for r in cfg.placement_table()))

    def test_rendered_recipe_carries_layout_pin(self):
        make_dir(self.d)
        rendered = render.render(load(self.d).view_for("alpha"), self.d / "deploy")
        text = next(v for k, v in rendered.items()
                    if k.startswith("sparkrun/recipes/")).decode()
        self.assertIn("layout:", text)
        self.assertIn("- host: 127.0.0.1", text)
        self.assertIn("ranks: [0]", text)


class TestEnvRendering(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        # ambient shell HF_TOKEN must never leak into hermetic renders
        self._env = mock.patch.dict(os.environ, {"HF_TOKEN": ""}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_model_env_and_injected_token_line_style(self):
        make_dir(self.d, recipes={"test-model": RECIPE_YAML +
                                  "env:\n  PYTHONUNBUFFERED: \"1\"\n"})
        rendered = render.render(load(self.d).view_for("alpha"), self.d / "deploy")
        text = next(v for k, v in rendered.items()
                    if k.startswith("sparkrun/recipes/")).decode()
        env = text.split("env:")[1].split("metadata:")[0]
        self.assertIn('  PYTORCH_CUDA_ALLOC_CONF: ""', env)
        self.assertIn("  PYTHONUNBUFFERED: '1'", env)
        self.assertIn('  HF_TOKEN: "test-hf-token"', env)
        # base keys first, model keys after, token last
        self.assertLess(env.index("PYTORCH_CUDA_ALLOC_CONF"),
                        env.index("PYTHONUNBUFFERED"))
        self.assertLess(env.index("PYTHONUNBUFFERED"), env.index("HF_TOKEN"))

    def test_repo_recipe_is_secret_free(self):
        make_dir(self.d)
        repo = (self.d / "recipes" / "test-model.yaml").read_text()
        self.assertNotIn("test-hf-token", repo)
        rendered = render.render(load(self.d).view_for("alpha"), self.d / "deploy")
        text = next(v for k, v in rendered.items()
                    if k.startswith("sparkrun/recipes/")).decode()
        self.assertIn("test-hf-token", text)

    def test_missing_secret_renders_no_token_line(self):
        make_dir(self.d)
        (self.d / ".env").write_text("")   # no HF_TOKEN anywhere
        with mock.patch.dict(os.environ, {"HF_TOKEN": ""}, clear=False):
            rendered = render.render(load(self.d).view_for("alpha"),
                                     self.d / "deploy")
        text = next(v for k, v in rendered.items()
                    if k.startswith("sparkrun/recipes/")).decode()
        self.assertNotIn("HF_TOKEN", text)


class TestPlacementTable(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_table_rows_and_gateway_names(self):
        make_dir(self.d, config_text=CONFIG_YAML.replace(
            "hosts: [alpha]", "hosts: [alpha, beta]"))
        rows = load(self.d).placement_table()
        self.assertEqual(rows, [
            ("alpha", "my-model", "test-gateway-name", True),
            ("beta", "my-model", "test-gateway-name", True)])

    def test_table_shows_unserved_host(self):
        rows = load(make_dir(Path(tempfile.mkdtemp()))).placement_table()
        self.assertEqual(rows, [
            ("alpha", "my-model", "test-gateway-name", True),
            ("beta", "", "", True)])

    def test_table_honors_host_override_gateway_name(self):
        make_dir(self.d, config_text=CONFIG_YAML.replace(
            "    hf_token_env: HF_TOKEN",
            "    host_overrides:\n      alpha:\n        litellm:\n"
            "          model_name: alpha-only-name"))
        rows = load(self.d).placement_table()
        self.assertEqual(rows[0], ("alpha", "my-model", "alpha-only-name", True))

    def test_base_only(self):
        cfg = load(make_dir(Path(tempfile.mkdtemp())))
        self.assertEqual(cfg.view_for("alpha").placement_table(), [])


class TestRepoRecipeCompliance(unittest.TestCase):
    """Every committed recipes/*.yaml stays a plain sparkrun recipe."""

    def test_all_repo_recipes_are_sparkrun_native(self):
        root = Path(__file__).resolve().parent.parent
        files = sorted((root / "recipes").glob("*.yaml"))
        self.assertTrue(files, "repo recipes/ directory missing")
        for path in files:
            data = config_mod.yaml.safe_load(path.read_text())
            self.assertIsInstance(data, dict, f"{path.name}: not a mapping")
            bad = [k for k in data if k not in recipe_mod.SPARKRUN_KNOWN_KEYS]
            self.assertEqual(bad, [], f"{path.name}: unknown top-level keys {bad}")
            self.assertEqual(str(data.get("name") or ""), path.stem,
                             f"{path.name}: inner name != file stem")
            self.assertTrue(str(data.get("model") or "").strip(),
                            f"{path.name}: missing model:")


if __name__ == "__main__":
    unittest.main(verbosity=2)
