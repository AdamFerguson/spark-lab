"""Phase 6a.5 tests: the system precheck (`check system` / `doctor` + init hook)."""

import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.commands import system, init  # noqa: E402
from tests.helpers import FakeRuntime  # noqa: E402

_ALL = {n for n, *_ in system.TOOLS}
_REQUIRED = {n for n, req, *_ in system.TOOLS if req}


def _rt(present):
    return FakeRuntime(available=set(present) | {"sh"})


class TestSystemCheck(unittest.TestCase):
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
        with contextlib.redirect_stdout(io.StringIO()):
            system.check(self._args(rt, install=True))
        self.assertTrue(any(a[0] == "sudo" and "get.docker.com" in " ".join(a) for a in rt.commands))

    def test_missing_required_without_command_flagged(self):
        rt = _rt(_ALL - {"python3"})   # python3 has no one-liner in the table
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            installed, failed = system.install(system.detect(rt), rt)
        self.assertIn("python3", failed)


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
