"""`expose` + `sync`: the gateway story for engines spark-lab didn't start."""

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

from sparklab.commands import apply as apply_cmd, litellm as litellm_cmd  # noqa: E402
from sparklab.commands import expose as expose_cmd, sync as sync_cmd  # noqa: E402
from tests.helpers import REFERENCE_ENV, SECRET_DUMMY, FakeRuntime, config_text  # noqa: E402

_AVAIL = {"sh", "sparkrun", "docker", "systemctl", "tailscale", "cloudflared"}

# vllm-fn runs by hand on the node's :8000, serving "manual-model"; the
# gateway serves only the config's own model.
CAPTURES = {
    "docker ps": (0, "ENGINE|vllm-fn|8000|manual-model\n"),
    "Authorization": (0, "my-spark-model\n"),
}


def _args(cp, runtime, **kw):
    base = dict(
        config=str(cp),
        hosts=None,
        dry_run=False,
        write=False,
        yes=False,
        port=None,
        served_model=None,
        public_name=None,
        verbose=False,
        json=False,
        runtime=runtime,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


class ExposeSyncBase(unittest.TestCase):
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

    def _config_extra_names(self):
        lit = yaml.safe_load(self.cp.read_text()).get("litellm") or {}
        return [m.get("model_name") for m in lit.get("extra_models") or []]


class TestExpose(ExposeSyncBase):
    def test_entry_shape_and_roundtrip(self):
        entry = expose_cmd.entry_for("manual-model", "10.0.0.5", 8000, "pretty")
        self.assertEqual(entry["model_name"], "pretty")
        self.assertEqual(entry["litellm_params"]["api_base"], "http://10.0.0.5:8000/v1")
        expose_cmd.add_extra_model(self.cp, entry)
        self.assertEqual(self._config_extra_names(), ["pretty"])
        with self.assertRaises(ValueError):  # duplicate name refused
            expose_cmd.add_extra_model(self.cp, entry)

    def test_expose_probe_then_writes_and_converges(self):
        with mock.patch.object(expose_cmd, "probe_engine", return_value=["manual-model"]) as probe:
            rc = expose_cmd.run(_args(self.cp, FakeRuntime(available=_AVAIL), host="mylab", dry_run=True))
            self.assertEqual(rc, 0)
            self.assertEqual(self._config_extra_names(), [])  # dry run: untouched
            rt = FakeRuntime(available=_AVAIL)
            rc = expose_cmd.run(_args(self.cp, rt, host="mylab"))
        self.assertEqual(rc, 0)
        # dry run + write both probed the host's engine (ip from config)
        probe.assert_called_with("http://127.0.0.1:8000")
        self.assertEqual(self._config_extra_names(), ["manual-model"])
        # gateway-only converge happened (compose up ran; no model launch)
        joined = [" ".join(c) for c in rt.commands]
        self.assertTrue(any("compose" in c and "up -d" in c for c in joined))
        self.assertFalse(any("sparkrun run" in c for c in joined))

    def test_expose_probe_failure_is_actionable(self):
        with mock.patch.object(expose_cmd, "probe_engine", side_effect=OSError("connection refused")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = expose_cmd.run(_args(self.cp, FakeRuntime(available=_AVAIL), host="mylab"))
        self.assertEqual(rc, 1)
        self.assertIn("engine probe failed", buf.getvalue())
        self.assertEqual(self._config_extra_names(), [])


class TestSync(ExposeSyncBase):
    def test_report_names_unexposed_engine(self):
        rt = FakeRuntime(available=_AVAIL, captures=CAPTURES)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sync_cmd.run(_args(self.cp, rt)), 0)
        out = buf.getvalue()
        self.assertIn("manual-model", out)
        self.assertIn("NOT EXPOSED", out)
        self.assertEqual(self._config_extra_names(), [])  # read-only

    def test_write_adds_entry_and_refreshes_state(self):
        rt = FakeRuntime(available=_AVAIL, captures=CAPTURES)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync_cmd.run(_args(self.cp, rt, write=True)), 0)
        self.assertEqual(self._config_extra_names(), ["manual-model"])
        entry = yaml.safe_load(self.cp.read_text())["litellm"]["extra_models"][0]
        self.assertEqual(entry["litellm_params"]["api_base"], "http://127.0.0.1:8000/v1")
        # node state adopted from on-disk reality
        self.assertTrue((self.d / ".sparklab-state" / "state.json").is_file())

    def test_write_converges_all_control_plane_hosts_not_just_selection(self):
        # config gains a second host with the control plane OFF; syncing only
        # 'mylab' must still converge the gateway on every CONTROL-PLANE host
        # (here: just mylab -- the off host is excluded from the apply scope).
        text = self.cp.read_text().replace(
            "hosts:\n  - name: mylab\n    remote: false\n    ip: 127.0.0.1\n",
            "hosts:\n  - name: mylab\n    remote: false\n    ip: 127.0.0.1\n"
            "  - name: quiet\n    remote: false\n    control_plane:\n      enabled: false\n",
        )
        self.cp.write_text(text)
        rt = FakeRuntime(available=_AVAIL, captures=CAPTURES)
        seen = {}
        real_apply = apply_cmd.run

        def spy(ns):
            seen["hosts"] = ns.hosts
            return real_apply(ns)

        with mock.patch.object(sync_cmd.apply_cmd, "run", spy), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync_cmd.run(_args(self.cp, rt, write=True)), 0)
        self.assertEqual(seen["hosts"], "mylab")  # control-plane hosts only


class TestLitellmVerb(ExposeSyncBase):
    def _apply(self, rt):
        ns = types.SimpleNamespace(
            config=str(self.cp),
            hosts=None,
            dry_run=False,
            restart_model=False,
            diff=False,
            no_model=False,
            verbose=False,
            json=False,
            runtime=rt,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(apply_cmd.run(ns), 0)

    def test_status_reports_health_and_served(self):
        self._apply(FakeRuntime(available=_AVAIL))
        rt = FakeRuntime(available=_AVAIL, captures=CAPTURES)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = litellm_cmd.run(_args(self.cp, rt, litellm_cmd="status"))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("in sync", out)
        self.assertIn("healthy: yes", out)
        self.assertIn("my-spark-model", out)
        self.assertFalse(any(c[0] == "docker" and "restart" in c for c in rt.commands))

    def test_restart_writes_stale_files_and_verifies(self):
        self._apply(FakeRuntime(available=_AVAIL))
        self.cp.write_text(self.cp.read_text().replace("model_name: my-spark-model", "model_name: renamed-gateway"))
        rt = FakeRuntime(available=_AVAIL, captures=CAPTURES)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = litellm_cmd.run(_args(self.cp, rt, litellm_cmd="restart"))
        self.assertEqual(rc, 0)
        joined = [" ".join(c) for c in rt.commands]
        self.assertTrue(any("compose" in c and "restart litellm" in c for c in joined))
        self.assertTrue(any("/health/liveliness" in c for c in joined))
        # the stale model_config was written from the config
        rendered = (self.install / "litellm" / "model_config.yaml").read_text()
        self.assertIn("renamed-gateway", rendered)
        # ... and the state now agrees (a second status is in sync)
        rt2 = FakeRuntime(available=_AVAIL, captures=CAPTURES)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            litellm_cmd.run(_args(self.cp, rt2, litellm_cmd="status"))
        self.assertIn("in sync", buf.getvalue())

    def test_restart_failure_leaves_state_untouched(self):
        self._apply(FakeRuntime(available=_AVAIL))
        self.cp.write_text(self.cp.read_text().replace("model_name: my-spark-model", "model_name: renamed-gateway"))
        state_file = self.d / ".sparklab-state" / "state.json"
        before = state_file.read_text()
        rt = FakeRuntime(available=_AVAIL, captures=CAPTURES, fail={"docker": 1})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = litellm_cmd.run(_args(self.cp, rt, litellm_cmd="restart"))
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT updated", buf.getvalue())
        self.assertEqual(state_file.read_text(), before)  # state NOT rewritten


if __name__ == "__main__":
    unittest.main(verbosity=2)
