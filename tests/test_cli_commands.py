"""Command-handler tests: init / status / teardown / upgrade via the runtime seam.

Covers the operational command paths (the thin wrappers around the runtime) so
the seam is exercised end-to-end for every subcommand, not just ``apply``.
"""

import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.commands import init, status, teardown, upgrade  # noqa: E402
from tests.helpers import REFERENCE_ENV, SECRET_DUMMY, FakeRuntime, config_text  # noqa: E402

_AVAIL = {"sparkrun", "docker", "systemctl", "tailscale", "cloudflared", sys.executable}


def _args(cp, runtime, **kw):
    base = dict(config=str(cp), dry_run=False, apply=False, yes=False,
                verbose=False, json=False, runtime=runtime, purge=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class TestCliCommands(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        self.install = self.d / "install"
        self.install.mkdir()
        self.cp = self.d / "config.yaml"
        self.cp.write_text(config_text(str(self.install)))
        (self.d / ".env").write_text(REFERENCE_ENV)

    def tearDown(self):
        self._env.stop()

    def test_status_issued(self):
        rt = FakeRuntime(available=_AVAIL)
        self.assertEqual(status.run(_args(self.cp, rt)), 0)
        self.assertTrue(any(a[0] == "sparkrun" and a[1] == "status" for a in rt.commands))
        self.assertTrue(any(a[0] == "docker" for a in rt.commands))
        self.assertTrue(any(a[0] == "tailscale" for a in rt.commands))

    def test_teardown_without_yes_refuses(self):
        rt = FakeRuntime(available=_AVAIL)
        self.assertEqual(teardown.run(_args(self.cp, rt, yes=False)), 1)
        self.assertEqual(rt.commands, [])

    def test_teardown_with_yes_stops_and_downs(self):
        rt = FakeRuntime(available=_AVAIL)
        self.assertEqual(teardown.run(_args(self.cp, rt, yes=True, purge=True)), 0)
        self.assertTrue(any(a == ["sparkrun", "stop", "qwen"] for a in rt.commands))
        self.assertTrue(any(a[0] == "docker" and "down" in a and "-v" in a for a in rt.commands))

    def test_upgrade_runs_pipeline_then_reapplies(self):
        rt = FakeRuntime(available=_AVAIL)
        self.assertEqual(upgrade.run(_args(self.cp, rt)), 0)
        # upgrade: refresh deps -> sparkrun update -> pull images -> re-apply
        self.assertTrue(any("pip" in " ".join(a) for a in rt.commands))
        self.assertTrue(any(a == ["sparkrun", "update"] for a in rt.commands))
        self.assertTrue(any(a[0] == "docker" and "pull" in a for a in rt.commands))
        # and the re-apply ensured the model
        self.assertTrue(any(a == ["sparkrun", "run", "qwen", "--ensure"] for a in rt.commands))

    def test_init_creates_config_and_generates_env(self):
        empty = Path(tempfile.mkdtemp())
        (empty / "config.example.yaml").write_text("model:\n  recipe_name: fresh\n")
        (empty / ".env.example").write_text("LITELLM_MASTER_KEY=\nLITELLM_SALT_KEY=\nHF_TOKEN=\n")
        a = types.SimpleNamespace(config=str(empty / "config.yaml"), yes=True,
                                  runtime=None, verbose=False, json=False)
        self.assertEqual(init.run(a), 0)
        self.assertTrue((empty / "config.yaml").is_file())
        self.assertRegex((empty / ".env").read_text(), r"LITELLM_MASTER_KEY=sk-[0-9a-f]{40}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
