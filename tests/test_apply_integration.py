"""Integration tests: the full ``apply`` path end-to-end against a FakeRuntime.

Exercises the real ``cmd_apply`` pipeline (load -> render -> build_plan ->
write_files -> execute) with a fake runtime + temp dirs: no node, no docker, no
sparkrun. This is the "mocked runtime seam" integration coverage (ADR 0002).

Tailscale is disabled in these configs so a converged re-apply is a true no-op.
"""

import contextlib
import io
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

from sparklab.commands import apply  # noqa: E402
from tests.helpers import REFERENCE_ENV, SECRET_DUMMY, FakeRuntime, config_text  # noqa: E402


def _args(config_path, runtime, dry=False, restart_model=False, diff=False):
    return types.SimpleNamespace(
        config=str(config_path), dry_run=dry, restart_model=restart_model, diff=diff,
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

    def test_renamed_recipe_keeps_stale_file_on_disk(self):
        """A recipe rename converges by stopping the old workload; the old
        recipe file is LEFT on disk (unmanaged) -- only the state record drops
        it. Re-scaling up re-renders the recipe over it."""
        rt = FakeRuntime()
        self.assertEqual(apply.run(_args(self.cfg_path, rt)), 0)
        old = self.install / "sparkrun" / "recipes" / "qwen.yaml"
        new = self.install / "sparkrun" / "recipes" / "qwen2.yaml"
        self.assertTrue(old.is_file())
        self.cfg_path.write_text(
            config_text(str(self.install)).replace("  qwen:", "  qwen2:"))
        self.assertEqual(apply.run(_args(self.cfg_path, rt, restart_model=True)), 0)
        self.assertTrue(old.exists(), "stale recipe file is kept on disk (unmanaged)")
        self.assertTrue(new.is_file())
        self.assertNotIn("sparkrun/recipes/qwen.yaml", self._state()["files"])
        self.assertIn("sparkrun/recipes/qwen2.yaml", self._state()["files"])

    def _state(self):
        return json.loads(self.state_file.read_text())

    def test_fresh_apply_writes_files_state_and_runs_commands(self):
        rt = FakeRuntime()
        self.assertEqual(apply.run(_args(self.cfg_path, rt)), 0)
        # files written into install_dir
        self.assertTrue((self.install / "sparkrun" / "recipes" / "qwen.yaml").is_file())
        self.assertTrue((self.install / "litellm" / "docker-compose.yml").is_file())
        # state recorded (files + the running model)
        st = self._state()
        self.assertIn("sparkrun/recipes/qwen.yaml", st["files"])
        self.assertEqual(st["model"]["name"], "qwen")
        # the exact commands issued: control plane first (available while the model
        # loads), then the model launched detached, then a bounded readiness probe
        compose = ["docker", "compose", "-f", str(self.install / "litellm" / "docker-compose.yml"),
                   "up", "-d", "--remove-orphans"]
        model_run = ["sh", "-c",
                     "echo $$ > /tmp/sparklab-model-launch.pid; exec sparkrun run "
                     + str(self.install / "sparkrun" / "recipes" / "qwen.yaml")
                     + " --ensure --hosts 127.0.0.1"]
        self.assertEqual(len(rt.commands), 3)
        self.assertEqual(rt.commands[0], compose)
        self.assertEqual(rt.commands[1], model_run)
        self.assertEqual(rt.commands[2][:2], ["sh", "-c"])
        self.assertIn("127.0.0.1:30000/health", rt.commands[2][2])
        # the probe crash-detects via the launch PID file and tails the launch log
        self.assertIn("kill -0", rt.commands[2][2])
        self.assertIn("/tmp/sparklab-model-launch.pid", rt.commands[2][2])
        self.assertIn("/tmp/sparklab-model-launch.log", rt.commands[2][2])
        # the detached launch captured its log for the probe to tail
        self.assertEqual(rt.spawn_logs, ["/tmp/sparklab-model-launch.log"])

    def test_reapply_is_idempotent_no_restart(self):
        apply.run(_args(self.cfg_path, FakeRuntime()))
        before = self._state()
        rt2 = FakeRuntime()
        self.assertEqual(apply.run(_args(self.cfg_path, rt2)), 0)
        # converged: no stop, no restart; the model is (re)ensured detached + a
        # bounded readiness probe runs. (The engine always ensures the model is up,
        # by design.) The control plane is skipped (unchanged).
        self.assertEqual(len(rt2.commands), 2)
        self.assertEqual(rt2.commands[0], ["sh", "-c",
                                        "echo $$ > /tmp/sparklab-model-launch.pid; exec sparkrun run "
                                        + str(self.install / "sparkrun" / "recipes" / "qwen.yaml")
                                        + " --ensure --hosts 127.0.0.1"])
        self.assertEqual(rt2.commands[1][:2], ["sh", "-c"])
        self.assertIn("127.0.0.1:30000/health", rt2.commands[1][2])
        self.assertFalse(any("stop" in argv for argv in rt2.commands))
        # recorded state is unchanged
        self.assertEqual(self._state(), before)

    def test_recipe_change_stays_pending_without_restart_flag(self):
        apply.run(_args(self.cfg_path, FakeRuntime()))
        before = self._state()["model"]["hash"]
        # mutate the recipe in config
        self.cfg_path.write_text(
            self.cfg_path.read_text().replace("mem_fraction_static: 0.85",
                                              "mem_fraction_static: 0.90"))
        rt = FakeRuntime()
        self.assertEqual(apply.run(_args(self.cfg_path, rt)), 0)
        # no stop issued, and state still records the OLD recipe hash (pending)
        self.assertFalse(any("stop" in argv for argv in rt.commands))
        self.assertEqual(self._state()["model"]["hash"], before)

    def test_recipe_change_converges_with_restart_model(self):
        apply.run(_args(self.cfg_path, FakeRuntime()))
        before = self._state()["model"]["hash"]
        self.cfg_path.write_text(
            self.cfg_path.read_text().replace("mem_fraction_static: 0.85",
                                              "mem_fraction_static: 0.90"))
        rt = FakeRuntime()
        self.assertEqual(apply.run(_args(self.cfg_path, rt, restart_model=True)), 0)
        # the running recipe was stopped, then started again
        self.assertTrue(any("sparkrun stop " +
                            str(self.install / "sparkrun" / "recipes" / "qwen.yaml") +
                            " --hosts 127.0.0.1" in " ".join(map(str, argv))
                            for argv in rt.commands))
        self.assertTrue(any("sparkrun run " in " ".join(map(str, argv))
                            and "--ensure" in " ".join(map(str, argv))
                            for argv in rt.commands))
        # state now records the NEW recipe hash
        self.assertNotEqual(self._state()["model"]["hash"], before)

    def test_dry_run_writes_nothing_and_runs_nothing(self):
        rt = FakeRuntime()
        self.assertEqual(apply.run(_args(self.cfg_path, rt, dry=True)), 0)
        self.assertEqual(rt.commands, [])
        self.assertFalse((self.install / "sparkrun" / "recipes" / "qwen.yaml").exists())
        self.assertFalse(self.state_file.exists())


    def test_dry_run_diff_shows_changes(self):
        apply.run(_args(self.cfg_path, FakeRuntime()))  # seed the on-disk install
        new_cfg = self.d / "config2.yaml"
        new_cfg.write_text(self.cfg_path.read_text().replace("qwen", "llama"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = apply.run(_args(new_cfg, FakeRuntime(), dry=True, diff=True))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("diff vs current install", out)
        self.assertIn("sparkrun/recipes/", out)
        # dry-run still wrote nothing new to the install tree
        self.assertFalse((self.install / "sparkrun" / "recipes" / "llama.yaml").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
