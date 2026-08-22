"""Integration tests: the full ``apply`` path end-to-end against a FakeRuntime.

Exercises the real ``cmd_apply`` pipeline (load -> render -> build_plan ->
write_files -> execute) with a fake runtime + temp dirs: no node, no docker, no
sparkrun. This is the "mocked runtime seam" integration coverage (ADR 0002).

Tailscale is disabled in these configs so a converged re-apply is a true no-op.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import cli  # noqa: E402
from tests.helpers import REFERENCE_ENV, SECRET_DUMMY, FakeRuntime, config_text  # noqa: E402


def _args(config_path, runtime, dry=False, apply=False):
    return types.SimpleNamespace(
        config=str(config_path), dry_run=dry, apply=apply, yes=apply,
        verbose=False, json=False, runtime=runtime,
    )


class TestApplyIntegration(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        self.install = self.d / "install"
        self.install.mkdir()
        self.cfg_path = self.d / "config.yaml"
        self.cfg_path.write_text(config_text(str(self.install)))  # tailscale off
        (self.d / ".env").write_text(REFERENCE_ENV)
        self.state_file = self.d / ".sparklab-state" / "state.json"

    def tearDown(self):
        self._env.stop()

    def _state(self):
        return json.loads(self.state_file.read_text())

    def test_fresh_apply_writes_files_state_and_runs_commands(self):
        rt = FakeRuntime()
        self.assertEqual(cli.cmd_apply(_args(self.cfg_path, rt)), 0)
        # files written into install_dir
        self.assertTrue((self.install / "sparkrun" / "recipes" / "qwen.yaml").is_file())
        self.assertTrue((self.install / "litellm" / "docker-compose.yml").is_file())
        # state recorded (files + the running model)
        st = self._state()
        self.assertIn("sparkrun/recipes/qwen.yaml", st["files"])
        self.assertEqual(st["model"]["name"], "qwen")
        # the exact commands issued: start the model, reconcile the stack
        self.assertEqual(
            rt.commands,
            [
                ["sparkrun", "run", "qwen", "--ensure"],
                ["docker", "compose", "-f", str(self.install / "litellm" / "docker-compose.yml"),
                 "up", "-d", "--remove-orphans"],
            ],
        )

    def test_reapply_is_idempotent_no_restart(self):
        cli.cmd_apply(_args(self.cfg_path, FakeRuntime()))
        before = self._state()
        rt2 = FakeRuntime()
        self.assertEqual(cli.cmd_apply(_args(self.cfg_path, rt2)), 0)
        # converged: no stop, no restart; only the idempotent "ensure running"
        # is issued. (The engine always ensures the model is up, by design.)
        self.assertEqual(rt2.commands, [["sparkrun", "run", "qwen", "--ensure"]])
        self.assertFalse(any("stop" in argv for argv in rt2.commands))
        # recorded state is unchanged
        self.assertEqual(self._state(), before)

    def test_recipe_change_stays_pending_without_apply(self):
        cli.cmd_apply(_args(self.cfg_path, FakeRuntime()))
        before = self._state()["model"]["hash"]
        # mutate the recipe in config
        self.cfg_path.write_text(
            self.cfg_path.read_text().replace("mem_fraction_static: 0.85",
                                              "mem_fraction_static: 0.90"))
        rt = FakeRuntime()
        self.assertEqual(cli.cmd_apply(_args(self.cfg_path, rt, apply=False)), 0)
        # no stop issued, and state still records the OLD recipe hash (pending)
        self.assertFalse(any("stop" in argv for argv in rt.commands))
        self.assertEqual(self._state()["model"]["hash"], before)

    def test_recipe_change_converges_with_apply(self):
        cli.cmd_apply(_args(self.cfg_path, FakeRuntime()))
        before = self._state()["model"]["hash"]
        self.cfg_path.write_text(
            self.cfg_path.read_text().replace("mem_fraction_static: 0.85",
                                              "mem_fraction_static: 0.90"))
        rt = FakeRuntime()
        self.assertEqual(cli.cmd_apply(_args(self.cfg_path, rt, apply=True)), 0)
        # the running recipe was stopped, then started again
        self.assertTrue(any(argv == ["sparkrun", "stop", "qwen"] for argv in rt.commands))
        self.assertTrue(any(argv == ["sparkrun", "run", "qwen", "--ensure"] for argv in rt.commands))
        # state now records the NEW recipe hash
        self.assertNotEqual(self._state()["model"]["hash"], before)

    def test_dry_run_writes_nothing_and_runs_nothing(self):
        rt = FakeRuntime()
        self.assertEqual(cli.cmd_apply(_args(self.cfg_path, rt, dry=True)), 0)
        self.assertEqual(rt.commands, [])
        self.assertFalse((self.install / "sparkrun" / "recipes" / "qwen.yaml").exists())
        self.assertFalse(self.state_file.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
