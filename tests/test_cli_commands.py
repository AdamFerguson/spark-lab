"""Command-handler tests: init / status / teardown / upgrade via the runtime seam.

Covers the operational command paths (the thin wrappers around the runtime) so
the seam is exercised end-to-end for every subcommand, not just ``apply``.
"""

import contextlib
import io
import os
import re
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

from sparklab.commands import (  # noqa: E402
    init, images, logs, migrate, status, teardown, upgrade, validate)
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
        self.assertTrue(any(a == ["sparkrun", "stop",
                                  str(self.install / "sparkrun" / "recipes" / "qwen.yaml"),
                                  "--hosts", "127.0.0.1"] for a in rt.commands))
        self.assertTrue(any(a[0] == "docker" and "down" in a and "-v" in a for a in rt.commands))

    def test_teardown_clears_state(self):
        rt = FakeRuntime(available=_AVAIL)
        st_dir = self.d / ".sparklab-state"
        st_dir.mkdir(parents=True, exist_ok=True)
        (st_dir / "state.json").write_text('{"files": {"a": "b"}}')
        self.assertTrue((st_dir / "state.json").exists())
        self.assertEqual(teardown.run(_args(self.cp, rt, yes=True)), 0)
        self.assertFalse((st_dir / "state.json").exists())   # cleared for a fresh re-apply

    def test_upgrade_without_yes_refuses(self):
        rt = FakeRuntime(available=_AVAIL)
        self.assertEqual(upgrade.run(_args(self.cp, rt, yes=False)), 1)
        self.assertEqual(rt.commands, [])

    def test_upgrade_runs_pipeline_then_reapplies(self):
        rt = FakeRuntime(available=_AVAIL)  # no uv -> pip refresh path
        self.assertEqual(upgrade.run(_args(self.cp, rt, yes=True)), 0)
        # upgrade: refresh deps (pip, uv absent) -> sparkrun update -> pull -> re-apply
        self.assertTrue(any("pip" in " ".join(a) and "install" in " ".join(a)
                            for a in rt.commands))
        self.assertTrue(any(a == ["sparkrun", "update"] for a in rt.commands))
        self.assertTrue(any(a[0] == "docker" and "pull" in a for a in rt.commands))
        # and the re-apply ensured the model
        self.assertTrue(any(a == ["sparkrun", "run", str(self.install / "sparkrun" / "recipes" / "qwen.yaml"),
                                  "--ensure", "--hosts", "127.0.0.1"] for a in rt.commands))

    def test_upgrade_refreshes_deps_via_uv_when_available(self):
        rt = FakeRuntime(available=_AVAIL | {"uv"})
        self.assertEqual(upgrade.run(_args(self.cp, rt, yes=True)), 0)
        self.assertTrue(any(a[:2] == ["uv", "lock"] for a in rt.commands))
        self.assertTrue(any(a[:2] == ["uv", "sync"] for a in rt.commands))
        self.assertFalse(any("pip" in " ".join(a) for a in rt.commands))

    def test_init_creates_config_and_generates_env(self):
        empty = Path(tempfile.mkdtemp())
        (empty / "config.example.yaml").write_text("model:\n  recipe_name: fresh\n")
        (empty / ".env.example").write_text("LITELLM_MASTER_KEY=\nLITELLM_SALT_KEY=\nHF_TOKEN=\n")
        a = types.SimpleNamespace(config=str(empty / "config.yaml"), yes=True,
                                  runtime=None, verbose=False, json=False)
        self.assertEqual(init.run(a), 0)
        self.assertTrue((empty / "config.yaml").is_file())
        self.assertRegex((empty / ".env").read_text(), r"LITELLM_MASTER_KEY=sk-[0-9a-f]{40}")

    def test_validate_ok_on_good_config(self):
        self.assertEqual(validate.run(_args(self.cp, FakeRuntime())), 0)

    def test_validate_fails_on_missing_secret(self):
        bad = self.d / "bad.yaml"
        # references a secret that is in neither .env nor the (pinned) env
        bad.write_text("install_dir: %s\nmodel:\n  recipe_name: qwen\n"
                       "litellm:\n  master_key_env: NOPE_MISSING_KEY\n" % self.install)
        self.assertEqual(validate.run(_args(bad, FakeRuntime())), 1)

    def test_logs_refuses_without_compose_file(self):
        a = _args(self.cp, FakeRuntime(available=_AVAIL),
                  service="litellm", lines=50, follow=False)
        self.assertEqual(logs.run(a), 1)

    def test_logs_builds_correct_argv(self):
        (self.install / "litellm").mkdir(parents=True, exist_ok=True)
        compose = self.install / "litellm" / "docker-compose.yml"
        compose.write_text("services: {}\n")
        rt = FakeRuntime(available=_AVAIL)
        a = _args(self.cp, rt, service="litellm", lines=50, follow=True)
        self.assertEqual(logs.run(a), 0)
        self.assertIn(["docker", "compose", "-f", str(compose), "logs",
                       "--tail", "50", "litellm", "--follow"], rt.commands)

    def test_check_images_reports_resolved(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = images.run(_args(self.cp, FakeRuntime(available=_AVAIL), probe=False))
        self.assertEqual(rc, 0)
        self.assertIn("check images", buf.getvalue())
        self.assertIn("litellm", buf.getvalue())

    def test_check_images_missing_model_refuses(self):
        bad = self.d / "noimg.yaml"
        bad.write_text("install_dir: %s\nversion: 2\nmodels:\n  m:\n    active: true\n"
                       "    hf_model: x\n" % self.install)
        self.assertEqual(images.run(_args(bad, FakeRuntime(available=_AVAIL), probe=False)), 1)

    def test_migrate_v1_to_v3_is_idempotent(self):
        v1 = self.d / "v1.yaml"
        v1.write_text("model:\n  recipe_name: q\n  hf_model: x\n  image: mm:1\n")
        a = lambda: types.SimpleNamespace(config=str(v1), dry_run=False, runtime=None,
                                          verbose=False, json=False)
        self.assertEqual(migrate.run(a()), 0)
        data = yaml.safe_load(v1.read_text())
        self.assertEqual(data["version"], 3)
        self.assertIn("q", data["models"])
        self.assertEqual(len(data["hosts"]), 1)
        self.assertEqual(migrate.run(a()), 0)  # already v3 -> no-op


if __name__ == "__main__":
    unittest.main(verbosity=2)
