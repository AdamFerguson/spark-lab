"""Model-swapping (zoo / llama-swap, ADR-0010): schema, render, gateway, plan, commands."""

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.commands import model as model_cmd, swap as swap_cmd, zoo as zoo_cmd  # noqa: E402
from sparklab.core import config as config_mod, converge, inventory, render, state  # noqa: E402
from tests.helpers import REFERENCE_ENV, SECRET_DUMMY, FakeRuntime  # noqa: E402

RECIPE = """\
name: {name}
model: test-llm/{name}
runtime: vllm
defaults:
  port: {port}
  served_model_name: test-llm/{name}
command: |
  vllm serve {{model}} --port {{port}}
"""

CLUSTER = """\
version: 3
install:
  name: zoo-lab
  install_dir: {install}
hosts:
  - name: sol
    ip: 10.0.4.27
  - name: luna
    ip: 10.0.4.171
    monitoring:
      role: exporters
      control_plane:
        enabled: false
swap:
  enabled: true
models:
  prod:
    active: true
    hosts: [sol]
    recipe: prodmodel
  trial:
    active: false
    hosts: [sol]
    recipe: trialmodel
    swap:
      enabled: true
      class: small
      aliases: [try-it]
  bigzoo:
    active: false
    hosts: [sol]
    recipe: bigmodel
    swap:
      enabled: true
      ttl: 0
litellm:
  model_name: prod-gateway
  port: 4000
  model_api_base_host: host.docker.internal
  master_key_env: LITELLM_MASTER_KEY
  salt_key_env: LITELLM_SALT_KEY
  db:
    user: litellm
    password_env: LITELLM_DB_PASSWORD
    db: litellm
  redis:
    enabled: false
"""


def _fixture(install=None, cluster_text=CLUSTER, extra_recipes=()):
    d = Path(tempfile.mkdtemp(prefix="sparklab-swap-test-"))
    (d / "recipes").mkdir()
    (d / "recipes" / "prodmodel.yaml").write_text(RECIPE.format(name="prodmodel", port=30000))
    (d / "recipes" / "trialmodel.yaml").write_text(RECIPE.format(name="trialmodel", port=8100))
    (d / "recipes" / "bigmodel.yaml").write_text(RECIPE.format(name="bigmodel", port=8200))
    for name in extra_recipes:
        (d / "recipes" / f"{name}.yaml").write_text(RECIPE.format(name=name, port=8300))
    (d / ".env").write_text(REFERENCE_ENV)
    cp = d / "config.yaml"
    cp.write_text(cluster_text.format(install=install or d / "install"))
    (Path(install) if install else d / "install").mkdir(parents=True, exist_ok=True)
    return d, cp


def _view(cp, host):
    cfg = config_mod.load(cp)
    return cfg, cfg.view_for(host)


class TestSchema(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()
        self.d, self.cp = _fixture()

    def tearDown(self):
        self._env.stop()

    def test_accessors_and_aliases(self):
        cfg, sol = _view(self.cp, "sol")
        self.assertTrue(cfg.swap_enabled())
        self.assertEqual(cfg.swap_port(), 9292)
        self.assertEqual(sol.swap_aliases(), ["trial", "bigzoo"])
        self.assertFalse(sol.swap_for_host() is False)
        luna = cfg.view_for("luna")
        self.assertEqual(luna.swap_aliases(), [])
        self.assertEqual(sol.swap_ttl("trial", sol.data["models"]["trial"]), 1800)
        self.assertEqual(sol.swap_ttl("bigzoo", sol.data["models"]["bigzoo"]), 0)

    def test_validation_matrix(self):
        cases = {
            # zoo model must be inactive
            "active": CLUSTER.replace("  trial:\n    active: false", "  trial:\n    active: true").replace(
                "  prod:\n    active: true", "  prod:\n    active: false"
            ),
            # spanning recipe (min_nodes 2) cannot join the zoo
            "spanning": CLUSTER.replace(
                "  trial:\n    active: false",
                "  trial:\n    active: false\n    min_nodes: 2",
            ),
            # engine port collision with the active model
            "port collision": CLUSTER.replace("  port: 4000", "  port: 4000").replace(
                "    recipe: trialmodel\n    swap:", "    recipe: trialmodel\n    port: 30000\n    swap:"
            ),
            # two zoo hosts
            "two hosts": CLUSTER.replace(
                "  trial:\n    active: false\n    hosts: [sol]",
                "  trial:\n    active: false\n    hosts: [sol, luna]",
            ),
            # zoo model without recipe
            "no recipe": CLUSTER.replace(
                "  bigzoo:\n    active: false\n    hosts: [sol]\n    recipe: bigmodel\n",
                "  bigzoo:\n    active: false\n    hosts: [sol]\n",
            ),
        }
        for label, text in cases.items():
            with self.subTest(label):
                d, cp = _fixture(cluster_text=text)
                with self.assertRaises(ValueError):
                    config_mod.load(cp)

    def test_swap_models_without_top_level_flag_are_inert(self):
        text = CLUSTER.replace("swap:\n  enabled: true", "swap:\n  enabled: false")
        d, cp = _fixture(cluster_text=text)
        cfg = config_mod.load(cp)  # loads fine; zoo declarations stay parked
        self.assertFalse(cfg.swap_enabled())
        sol = cfg.view_for("sol")
        self.assertEqual(sol.swap_aliases(), [])
        rendered = render.render(sol, Path(tempfile.mkdtemp()))
        self.assertNotIn("llama-swap/config.yaml", rendered)
        self.assertNotIn("sparkrun/recipes/trial.yaml", rendered)

    def test_model_up_down_reject_zoo_models(self):
        args = types.SimpleNamespace(config=str(self.cp), hosts=None, model="trial", runtime=FakeRuntime(), yes=True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(model_cmd.up(args), 1)
            self.assertEqual(model_cmd.down(args), 1)
        self.assertIn("llama-swap owns its lifecycle", buf.getvalue())


class TestRender(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()
        self.d, self.cp = _fixture()

    def tearDown(self):
        self._env.stop()

    def test_swap_files_rendered_on_zoo_host_only(self):
        cfg, sol = _view(self.cp, "sol")
        rendered = render.render(sol, Path(tempfile.mkdtemp()))
        self.assertIn("llama-swap/config.yaml", rendered)
        self.assertIn("llama-swap/llama-swap.service", rendered)
        self.assertIn("sparkrun/recipes/trial.yaml", rendered)  # node recipe llama-swap launches
        self.assertIn("sparkrun/recipes/bigzoo.yaml", rendered)
        # luna (no swap models, exporters) gets neither
        luna = cfg.view_for("luna")
        rendered_l = render.render(luna, Path(tempfile.mkdtemp()))
        self.assertNotIn("llama-swap/config.yaml", rendered_l)
        self.assertNotIn("sparkrun/recipes/trial.yaml", rendered_l)

    def test_llama_swap_config_content(self):
        cfg, sol = _view(self.cp, "sol")
        text = render.render(sol, Path(tempfile.mkdtemp()))["llama-swap/config.yaml"].decode()
        data = yaml.safe_load(text.replace("{install_dir}", "/opt/sparklab"))
        self.assertEqual(data["healthCheckTimeout"], 720)  # 600 default + 120 headroom
        self.assertEqual(data["globalTTL"], 0)
        # llama-swap keys = the id litellm forwards (engine served id);
        # the public/gateway name rides along as an alias.
        trial = data["models"]["test-llm/trialmodel"]
        self.assertEqual(trial["name"], "trial")
        self.assertEqual(trial["ttl"], 1800)
        self.assertEqual(trial["proxy"], "http://127.0.0.1:8100")  # port from its recipe defaults
        self.assertIn("No running workload matches intent", trial["cmdStop"])
        self.assertIn("--ensure", trial["cmd"])
        self.assertIn("/opt/sparklab/sparkrun/recipes/trial.yaml", trial["cmd"])
        self.assertIn("--hosts 10.0.4.27", trial["cmd"])
        self.assertEqual(trial["useModelName"], "test-llm/trialmodel")
        self.assertEqual(trial["aliases"], ["trial", "try-it"])  # public name joins swap aliases
        self.assertEqual(data["models"]["test-llm/bigmodel"]["ttl"], 0)

    def test_zoo_node_recipe_has_no_layout_pin(self):
        cfg, sol = _view(self.cp, "sol")
        rendered = render.render(sol, Path(tempfile.mkdtemp()))
        recipe = yaml.safe_load(rendered["sparkrun/recipes/trial.yaml"])
        self.assertNotIn("layout", recipe)  # scheduler placement via --hosts, not a pin
        self.assertEqual(recipe["model"], "test-llm/trialmodel")
        # the ACTIVE model's recipe still carries its pin (single-node -> local)
        prod = yaml.safe_load(rendered["sparkrun/recipes/prod.yaml"])
        self.assertEqual(prod["layout"]["placements"][0]["host"], "127.0.0.1")

    def test_unit_uses_absolute_paths(self):
        cfg, sol = _view(self.cp, "sol")
        unit = render.render(sol, Path(tempfile.mkdtemp()))["llama-swap/llama-swap.service"].decode()
        install = str(Path(self.d) / "install")
        self.assertIn(f"ExecStart={install}/bin/llama-swap --config {install}/llama-swap/config.yaml", unit)
        self.assertIn("--listen 0.0.0.0:9292", unit)

    def test_gateway_entries_merged_into_extra_models(self):
        cfg, sol = _view(self.cp, "sol")
        text = render.render(sol, Path(tempfile.mkdtemp()))["litellm/extra_models.yaml"].decode()
        data = yaml.safe_load(text)
        names = [m["model_name"] for m in data["model_list"]]
        self.assertEqual(sorted(names), ["bigzoo", "trial"])
        trial = next(m for m in data["model_list"] if m["model_name"] == "trial")
        self.assertEqual(trial["litellm_params"]["api_base"], "http://host.docker.internal:9292/v1")
        self.assertEqual(trial["litellm_params"]["timeout"], 900)  # 600 readiness + 300
        self.assertEqual(trial["litellm_params"]["model"], "custom_openai/test-llm/trialmodel")

    def test_write_expands_install_dir_in_zoo_files(self):
        out = converge.expand_install_dir("llama-swap/config.yaml", b"x {install_dir}/y", "/node/ai")
        self.assertEqual(out, b"x /node/ai/y")


class TestGatewayConflicts(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_zoo_gateway_name_collision(self):
        # prod's gateway name collides with a zoo model's public name
        text = CLUSTER.replace("model_name: prod-gateway", "model_name: trial")
        d, cp = _fixture(cluster_text=text)
        cfg = config_mod.load(cp)
        problems = cfg.serving_conflicts()
        self.assertTrue(any("same name" in p for p in problems), problems)


class TestConvergePlan(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()
        self.d, self.cp = _fixture()

    def tearDown(self):
        self._env.stop()

    def test_swap_config_change_restarts_llama_swap_best_effort(self):
        cfg, sol = _view(self.cp, "sol")
        rendered = render.render(sol, Path(tempfile.mkdtemp()))
        state_files = converge.compute_files_after_apply(rendered)
        # flip one zoo model's ttl => config.yaml changes
        changed = dict(rendered)
        changed["llama-swap/config.yaml"] = rendered["llama-swap/config.yaml"].replace(b"ttl: 1800", b"ttl: 300")
        plan = converge.build_plan(
            sol,
            changed,
            state_files,
            {"name": "prod", "hash": state.sha256_bytes(rendered["sparkrun/recipes/prod.yaml"])},
            allow_restart=False,
        )
        descs = [d for d, _ in plan.commands]
        self.assertIn("Ensure llama-swap service running (zoo)", descs)
        self.assertIn("Restart llama-swap (zoo config changed)", descs)
        self.assertIn("Verify llama-swap is healthy", descs)
        self.assertIn("Restart llama-swap (zoo config changed)", plan.best_effort)
        restart = dict(plan.commands)["Restart llama-swap (zoo config changed)"]
        self.assertIn("XDG_RUNTIME_DIR", " ".join(restart))

    def test_clean_plan_has_no_restart_but_ensures(self):
        cfg, sol = _view(self.cp, "sol")
        rendered = render.render(sol, Path(tempfile.mkdtemp()))
        state_files = converge.compute_files_after_apply(rendered)
        plan = converge.build_plan(
            sol,
            rendered,
            state_files,
            {"name": "prod", "hash": state.sha256_bytes(rendered["sparkrun/recipes/prod.yaml"])},
            allow_restart=False,
        )
        descs = [d for d, _ in plan.commands]
        self.assertIn("Ensure llama-swap service running (zoo)", descs)
        self.assertNotIn("Restart llama-swap (zoo config changed)", descs)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()
        self.d, self.cp = _fixture()

    def tearDown(self):
        self._env.stop()

    def _swap_args(self, verb, model=None, yes=False, runtime=None):
        return types.SimpleNamespace(
            config=str(self.cp),
            hosts="sol",
            model=model,
            yes=yes,
            swap_cmd=verb,
            runtime=runtime or FakeRuntime(captures={"running": (0, '{"models":[{"id":"trial"}]}')}),
        )

    def test_swap_status_lists_resident(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(swap_cmd.run(self._swap_args("status")), 0)
        self.assertIn("resident: trial", buf.getvalue())

    def test_unload_all_requires_yes(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(swap_cmd.run(self._swap_args("unload")), 1)
        self.assertIn("--yes", buf.getvalue())

    def test_unload_named_posts_endpoint(self):
        rt = FakeRuntime(captures={"unload/trial": (0, "ok"), "running": (0, "{}")})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(swap_cmd.run(self._swap_args("unload", model="trial", runtime=rt)), 0)
        self.assertTrue(any("unload/trial" in " ".join(c) for c in rt.calls))

    def test_unload_unknown_model_refused(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(swap_cmd.run(self._swap_args("unload", model="nothere")), 1)
        self.assertIn("not a swap model", buf.getvalue())

    def test_zoo_prepare_without_binary_fails_actionably(self):
        rt = FakeRuntime(available={"sh", "curl"}, fail={"sh": 1})
        args = types.SimpleNamespace(config=str(self.cp), hosts="sol", runtime=rt)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(zoo_cmd.run(args), 1)
        self.assertIn("llama-swap binary not found", buf.getvalue())

    def test_zoo_prepare_success_path(self):
        # no failures: binary test + unit install + health all succeed
        rt = FakeRuntime(available={"sh", "curl", "systemctl"})
        args = types.SimpleNamespace(config=str(self.cp), hosts="sol", runtime=rt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(zoo_cmd.run(args), 0)
        self.assertIn("llama-swap ready on sol:9292", buf.getvalue())
        self.assertTrue(any("enable --now llama-swap" in " ".join(c) for c in rt.calls))
        # unit + zoo files landed in the install dir
        self.assertTrue((Path(str(self.d)) / "install" / "llama-swap" / "llama-swap.service").exists())


class TestInventory(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, SECRET_DUMMY)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_swap_resident_shape_tolerant(self):
        self.assertEqual(inventory.swap_resident('[{"id": "a"}, "b"]'), ["a", "b"])
        self.assertEqual(inventory.swap_resident('{"models": ["x"]}'), ["x"])
        self.assertEqual(inventory.swap_resident("not json"), [])

    def test_discover_includes_swap_on_zoo_host(self):
        d, cp = _fixture()
        cfg, sol = _view(cp, "sol")
        rt = FakeRuntime(
            available={"docker", "sh"}, captures={"running": (0, '{"models":[{"id":"trial"}]}'), "v1/models": (0, "")}
        )
        out = inventory.discover(rt, sol)
        self.assertEqual(out["swap"]["resident"], ["trial"])
        self.assertTrue(out["swap"]["reachable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
