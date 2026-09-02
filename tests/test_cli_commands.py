"""Command-handler tests: init / status / teardown / check / logs via the
runtime seam.

Covers the operational command paths (the thin wrappers around the runtime) so
the seam is exercised end-to-end for every subcommand, not just ``apply``.
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

from sparklab.commands import (  # noqa: E402
    init,
    logs,
    status,
    teardown,
    check as check_cmd,
)
from tests.helpers import REFERENCE_ENV, SECRET_DUMMY, FakeRuntime, config_text  # noqa: E402

_AVAIL = {"sh", "sparkrun", "docker", "systemctl", "tailscale", "cloudflared", sys.executable}


def _args(cp, runtime, **kw):
    base = dict(
        config=str(cp), dry_run=False, apply=False, yes=False, verbose=False, json=False, runtime=runtime, purge=False
    )
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

    def test_status_json_reports_live_inventory(self):
        rt = FakeRuntime(
            available=_AVAIL,
            captures={
                "docker ps": (0, "ENGINE|vllm-fn|8000|manual-model\n"),
                "Authorization": (0, "my-spark-model\n"),
            },
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(status.run(_args(self.cp, rt, json=True)), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["hosts"]["mylab"]["engines"][0]["container"], "vllm-fn")
        self.assertEqual(data["hosts"]["mylab"]["engines"][0]["port"], 8000)
        self.assertEqual(data["hosts"]["mylab"]["gateway"]["served"], ["my-spark-model"])
        self.assertEqual(data["placement"][0]["model"], "qwen")

    def test_teardown_without_yes_refuses(self):
        rt = FakeRuntime(available=_AVAIL)
        self.assertEqual(teardown.run(_args(self.cp, rt, yes=False)), 1)
        self.assertEqual(rt.commands, [])

    def test_teardown_with_yes_stops_and_downs(self):
        rt = FakeRuntime(available=_AVAIL)
        self.assertEqual(teardown.run(_args(self.cp, rt, yes=True, purge=True)), 0)
        self.assertTrue(
            any(
                "sparkrun stop " + str(self.install / "sparkrun" / "recipes" / "qwen.yaml") + " --hosts 127.0.0.1"
                in " ".join(map(str, a))
                for a in rt.commands
            )
        )
        self.assertTrue(any(a[0] == "docker" and "down" in a and "-v" in a for a in rt.commands))

    def test_teardown_refusal_names_the_kept_volumes(self):
        rt = FakeRuntime(available=_AVAIL)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(teardown.run(_args(self.cp, rt, yes=False)), 1)
        out = buf.getvalue()
        self.assertIn("KEPT", out)
        self.assertIn("litellm_postgres_data", out)
        self.assertIn("litellm_redis_data", out)

    def test_teardown_purge_warns_about_destroyed_volumes(self):
        rt = FakeRuntime(available=_AVAIL)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(teardown.run(_args(self.cp, rt, yes=True, purge=True)), 0)
        out = buf.getvalue()
        self.assertIn("DESTROYS", out)
        self.assertIn("litellm_postgres_data", out)
        self.assertIn("UNRECOVERABLE", out)

    def test_teardown_clears_state(self):
        rt = FakeRuntime(available=_AVAIL)
        st_dir = self.d / ".sparklab-state"
        st_dir.mkdir(parents=True, exist_ok=True)
        (st_dir / "state.json").write_text('{"files": {"a": "b"}}')
        self.assertTrue((st_dir / "state.json").exists())
        self.assertEqual(teardown.run(_args(self.cp, rt, yes=True)), 0)
        self.assertFalse((st_dir / "state.json").exists())  # cleared for a fresh re-apply

    def test_init_creates_config_and_generates_env(self):
        empty = Path(tempfile.mkdtemp())
        (empty / "config.example.yaml").write_text("model:\n  recipe_name: fresh\n")
        (empty / ".env.example").write_text("LITELLM_MASTER_KEY=\nLITELLM_SALT_KEY=\nHF_TOKEN=\n")
        a = types.SimpleNamespace(config=str(empty / "config.yaml"), yes=True, runtime=None, verbose=False, json=False)
        self.assertEqual(init.run(a), 0)
        self.assertTrue((empty / "config.yaml").is_file())
        self.assertRegex((empty / ".env").read_text(), r"LITELLM_MASTER_KEY=sk-[0-9a-f]{40}")

    def test_check_ok_on_good_config(self):
        self.assertEqual(check_cmd.run(_args(self.cp, FakeRuntime())), 0)

    def test_check_fails_on_unsupported_config(self):
        bad = self.d / "bad.yaml"
        # retired schema -> load fails -> invalid pre-flight
        bad.write_text("install:\n  install_dir: %s\nmodel:\n  recipe_name: qwen\n" % self.install)
        self.assertEqual(check_cmd.run(_args(bad, FakeRuntime())), 1)

    def test_logs_refuses_without_compose_file(self):
        a = _args(self.cp, FakeRuntime(available=_AVAIL), service="litellm", lines=50, follow=False)
        self.assertEqual(logs.run(a), 1)

    def test_logs_builds_correct_argv(self):
        (self.install / "litellm").mkdir(parents=True, exist_ok=True)
        compose = self.install / "litellm" / "docker-compose.yml"
        compose.write_text("services: {}\n")
        rt = FakeRuntime(available=_AVAIL)
        a = _args(self.cp, rt, service="litellm", lines=50, follow=True)
        self.assertEqual(logs.run(a), 0)
        self.assertIn(
            ["docker", "compose", "-f", str(compose), "logs", "--tail", "50", "litellm", "--follow"], rt.commands
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
