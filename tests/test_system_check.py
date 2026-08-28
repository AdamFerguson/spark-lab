"""Phase 6a.5 tests: the system precheck (`check system` / `doctor` + init hook)."""

import contextlib
import io
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

from sparklab.commands import system, init  # noqa: E402
from tests.helpers import FakeRuntime, REFERENCE_CONFIG  # noqa: E402

_ALL = {n for n, *_ in system.TOOLS}
_REQUIRED = {n for n, req, *_ in system.TOOLS if req}


def _rt(present):
    return FakeRuntime(available=set(present) | {"sh"})


class _ConfigIsolated(unittest.TestCase):
    """Run with a hermetic config.yaml in a temp cwd.

    ``check`` fans out over the config's hosts; a v3 cluster config at the repo
    root would route these tests to REAL remote hosts, ignoring the injected
    FakeRuntime. A v1 single-host config keeps the fake-runtime path.
    """

    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / "config.yaml").write_text(REFERENCE_CONFIG)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()
        super().tearDown()


class TestSystemCheck(_ConfigIsolated):
    def _out(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = system.check(args)
        return rc, buf.getvalue()

    def _args(self, rt, **kw):
        base = dict(runtime=rt, config="config.yaml", install=False, all=False,
                    yes=False, verbose=False, json=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_all_present(self):
        rc, out = self._out(self._args(_rt(_ALL)))
        self.assertEqual(rc, 0)
        self.assertIn("All required tools are present", out)

    def test_missing_required_reported(self):
        rc, out = self._out(self._args(_rt(_ALL - {"uv", "sparkrun"})))
        self.assertEqual(rc, 1)
        self.assertIn("MISSING", out)
        self.assertIn("uv", out)
        self.assertIn("sparkrun", out)

    def test_install_issues_commands(self):
        rt = _rt(_ALL - {"uv", "sparkrun"})
        rc, out = self._out(self._args(rt, install=True))
        # uv + sparkrun install commands were issued through the seam
        self.assertTrue(any(a[0] == "sh" and any("astral.sh/uv/install.sh" in x for x in a)
                            for a in rt.commands))
        self.assertTrue(any("uv tool install sparkrun" in " ".join(a) for a in rt.commands))
        self.assertEqual(rc, 0)  # installed (rc 0) even though not yet on this shell's PATH

    def test_install_sudo_tool_uses_sudo(self):
        rt = _rt(_ALL - {"docker"})
        sudo_calls = []
        with mock.patch.object(rt, "run_sudo", wraps=rt.run_sudo) as spy:
            with contextlib.redirect_stdout(io.StringIO()):
                system.check(self._args(rt, install=True))
        self.assertTrue(any(a[0] == "sudo" and "get.docker.com" in " ".join(a) for a in rt.commands))
        # the sudo-flagged tool went through the run_sudo seam exactly once;
        # non-sudo installs never do
        self.assertEqual(len(spy.call_args_list), 1)
        self.assertIn("get.docker.com", " ".join(spy.call_args_list[0].args[0]))

    def test_missing_required_without_command_flagged(self):
        rt = _rt(_ALL - {"python3"})   # python3 has no one-liner in the table
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            installed, failed = system.install(system.detect(rt), rt)
        self.assertIn("python3", failed)


class TestCapabilities(_ConfigIsolated):
    """The capability layer: docker access (current user can reach the daemon)."""

    def _args(self, rt, **kw):
        base = dict(runtime=rt, config="config.yaml", install=False, all=False,
                    yes=False, verbose=False, json=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def _out(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = system.check(args)
        return rc, buf.getvalue()

    def test_docker_access_ok_when_reachable(self):
        rt = FakeRuntime(available=_ALL | {"sh"})
        caps = system.check_capabilities(rt)
        dc = [c for c in caps if c["name"] == "docker access"]
        self.assertEqual(len(dc), 1)
        self.assertTrue(dc[0]["ok"])

    def test_docker_access_flagged_when_denied(self):
        rt = FakeRuntime(available=_ALL | {"sh"}, fail={"docker": 1})
        dc = [c for c in system.check_capabilities(rt) if c["name"] == "docker access"][0]
        self.assertFalse(dc["ok"])
        self.assertTrue(dc["required"])
        self.assertIn("usermod", dc["fix"])

    def test_check_exits_1_and_reports_when_docker_denied(self):
        rt = FakeRuntime(available=_ALL | {"sh"}, fail={"docker": 1})
        rc, out = self._out(self._args(rt))
        self.assertEqual(rc, 1)
        self.assertIn("NEEDS FIX", out)
        self.assertIn("docker access", out)

    def test_no_capability_when_docker_absent(self):
        rt = FakeRuntime(available=(_ALL - {"docker"}) | {"sh"})
        self.assertNotIn("docker access", [c["name"] for c in system.check_capabilities(rt)])


class TestDoctorAlias(unittest.TestCase):
    def test_doctor_runs(self):
        from sparklab import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["doctor"])   # uses the real runtime; rc reflects this machine
        self.assertIn(rc, (0, 1))
        self.assertIn("system check", buf.getvalue())


class TestInitPrecheck(unittest.TestCase):
    def test_init_reports_missing_tools_and_still_creates_config(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.example.yaml").write_text("model:\n  recipe_name: fresh\n")
        (d / ".env.example").write_text("LITELLM_MASTER_KEY=\nLITELLM_SALT_KEY=\n")
        rt = _rt(_ALL - {"uv"})
        a = types.SimpleNamespace(config=str(d / "config.yaml"), yes=True, runtime=rt,
                                  verbose=False, json=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = init.run(a)
        self.assertEqual(rc, 0)
        self.assertTrue((d / "config.yaml").is_file())   # config still created
        self.assertIn("MISSING", buf.getvalue())
        self.assertIn("check system --install", buf.getvalue())

    def test_init_precheck_skipped_without_runtime(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.example.yaml").write_text("model:\n  recipe_name: fresh\n")
        (d / ".env.example").write_text("LITELLM_MASTER_KEY=\n")
        a = types.SimpleNamespace(config=str(d / "config.yaml"), yes=True,
                                  runtime=None, verbose=False, json=False)
        self.assertEqual(init.run(a), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
