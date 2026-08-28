"""`model up` / `model down` scale tests + `init --hosts` bootstrap (ADR 0008).

All hermetic: a v3 config with two LOCAL hosts (remote: false) + a recording
FakeRuntime; the config file round-trip is asserted on disk. The install dir is
rewritten into the test's temp dir so a real converge writes nowhere real.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.commands import init as init_cmd, model as model_cmd  # noqa: E402
from sparklab.core import state as state_mod  # noqa: E402
from tests.helpers import FakeRuntime, REFERENCE_ENV, SECRET_DUMMY, V3_CLUSTER_CONFIG  # noqa: E402

# Two local hosts: bootstrap/scale tests need no SSH at all.
LOCAL_V3 = V3_CLUSTER_CONFIG.replace(
    "  - name: beta\n    ssh: beta.tailx.ts.net\n    remote: true\n",
    "  - name: beta\n    remote: false\n", 1)


def _args(cp, runtime, **kw):
    base = dict(config=cp, hosts=None, dry_run=False, apply=False, restart_model=False,
                yes=False, all=False, verbose=False, json=False, runtime=runtime)
    base.update(kw)
    return SimpleNamespace(**base)


class ScaleBase(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        self.install = self.d / "install"

    def tearDown(self):
        self._env.stop()

    def _setup(self, text=None):
        text = (text or LOCAL_V3).replace("/opt/sparklab", str(self.install))
        (self.d / "config.yaml").write_text(text)
        (self.d / ".env").write_text(REFERENCE_ENV)
        return str(self.d / "config.yaml")


class TestModelUp(ScaleBase):
    def test_up_adds_host_to_config_and_converges_only_it(self):
        cp = self._setup(LOCAL_V3.replace("hosts: [alpha, beta]", "hosts: [alpha]", 1).replace(
            "/opt/sparklab", str(self.install)))
        rt = FakeRuntime()
        rc = model_cmd.up(_args(cp, rt, model="qwen", hosts="beta"))
        self.assertEqual(rc, 0)
        data = yaml.safe_load((self.d / "config.yaml").read_text())
        self.assertEqual(data["models"]["qwen"]["hosts"], ["alpha", "beta"])
        self.assertTrue(data["models"]["qwen"].get("active"))
        # converged only beta: the detached ensure ran for the recipe
        self.assertTrue(any(a[:5] == ["sparkrun", "run",
                                      str(self.install / "sparkrun" / "recipes" / "qwen.yaml"),
                                      "--ensure", "--hosts"] for a in rt.calls))

    def test_up_without_hosts_adds_all_remaining(self):
        text = LOCAL_V3.replace("hosts: [alpha, beta]", "hosts: [alpha]", 1)
        cp = self._setup(text)
        model_cmd.up(_args(cp, FakeRuntime(), model="qwen"))
        data = yaml.safe_load((self.d / "config.yaml").read_text())
        self.assertEqual(data["models"]["qwen"]["hosts"], ["alpha", "beta"])

    def test_up_already_everywhere_is_a_noop(self):
        cp = self._setup()
        before = (self.d / "config.yaml").read_text()
        rc = model_cmd.up(_args(cp, FakeRuntime(), model="qwen"))
        self.assertEqual(rc, 0)
        self.assertEqual((self.d / "config.yaml").read_text(), before)

    def test_up_refuses_host_conflict(self):
        # qwen serves alpha; llama (active) serves beta. Adding beta to qwen
        # creates the conflict -- at operation time, not load time.
        text = LOCAL_V3.replace("hosts: [alpha, beta]", "hosts: [alpha]", 1).replace(
            "litellm:\n  model_name:",
            "  llama:\n    active: true\n    hosts: [beta]\n"
            "    hf_model: test-llm/llama\n    image: lmsysorg/sglang:llama\n"
            "litellm:\n  model_name:", 1)
        cp = self._setup(text)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = model_cmd.up(_args(cp, FakeRuntime(), model="qwen", hosts="beta"))
        self.assertEqual(rc, 1)
        self.assertIn("llama", buf.getvalue())
        # the config is untouched
        self.assertEqual(yaml.safe_load((self.d / "config.yaml").read_text())["models"][
            "qwen"]["hosts"], ["alpha"])

    def test_up_unknown_model_refuses(self):
        cp = self._setup()
        self.assertEqual(model_cmd.up(_args(cp, FakeRuntime(), model="nope", hosts="beta")), 1)

    def test_up_refuses_control_plane_off_host(self):
        text = LOCAL_V3.replace("hosts: [alpha, beta]", "hosts: [alpha]", 1).replace(
            "    monitoring:\n      instance_label: beta-node",
            "    control_plane:\n      enabled: false\n"
            "    monitoring:\n      instance_label: beta-node")
        cp = self._setup(text)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = model_cmd.up(_args(cp, FakeRuntime(), model="qwen", hosts="beta"))
        self.assertEqual(rc, 1)
        self.assertIn("control_plane", buf.getvalue())
        self.assertEqual(yaml.safe_load((self.d / "config.yaml").read_text())["models"][
            "qwen"]["hosts"], ["alpha"])

    def test_up_requires_v3(self):
        from tests.helpers import config_text
        (self.d / "v1.yaml").write_text(config_text(str(self.install)))
        (self.d / "v1env").write_text(REFERENCE_ENV)
        # .env must sit next to the config: use its own dir
        sub = self.d / "v1dir"
        sub.mkdir()
        (sub / "config.yaml").write_text(config_text(str(self.install)))
        (sub / ".env").write_text(REFERENCE_ENV)
        self.assertEqual(model_cmd.up(_args(str(sub / "config.yaml"), FakeRuntime(),
                                            model="qwen")), 1)


class TestModelDown(ScaleBase):
    def _seed_state(self, cp):
        """Record that beta is running the current recipe (so down has work)."""
        from sparklab.core import config as config_mod, render
        cfg = config_mod.load(cp)
        st = state_mod.State(Path(cp).parent / ".sparklab-state")
        rendered = render.render(cfg.view_for("beta"), self.d / "deploy-b")
        st.set_state({rel: state_mod.sha256_bytes(data) for rel, data in rendered.items()},
                     {"name": "qwen", "hash": state_mod.sha256_bytes(
                         rendered["sparkrun/recipes/qwen.yaml"])})

    def test_down_without_yes_refuses(self):
        cp = self._setup()
        before = (self.d / "config.yaml").read_text()
        rt = FakeRuntime()
        self.assertEqual(model_cmd.down(_args(cp, rt, model="qwen", hosts="beta")), 1)
        self.assertEqual(rt.calls, [])
        self.assertEqual((self.d / "config.yaml").read_text(), before)

    def test_down_removes_host_and_stops_model(self):
        cp = self._setup()
        self._seed_state(cp)
        rt = FakeRuntime()
        rc = model_cmd.down(_args(cp, rt, model="qwen", hosts="beta", yes=True))
        self.assertEqual(rc, 0)
        data = yaml.safe_load((self.d / "config.yaml").read_text())
        self.assertEqual(data["models"]["qwen"]["hosts"], ["alpha"])
        # the stale workload was stopped on beta (gated stop, deliberately allowed)
        self.assertTrue(any(a[:3] == ["sparkrun", "stop",
                                      str(self.install / "sparkrun" / "recipes" / "qwen.yaml")]
                            for a in rt.calls))

    def test_down_everywhere_removes_active(self):
        cp = self._setup()
        self._seed_state(cp)
        model_cmd.down(_args(cp, FakeRuntime(), model="qwen", yes=True))
        data = yaml.safe_load((self.d / "config.yaml").read_text())
        self.assertEqual(data["models"]["qwen"]["hosts"], [])
        self.assertNotIn("active", data["models"]["qwen"])

    def test_down_refuses_unknown_hosts(self):
        cp = self._setup()
        rc = model_cmd.down(_args(cp, FakeRuntime(), model="qwen", hosts="gamma", yes=True))
        self.assertEqual(rc, 1)


class TestInitBootstrap(ScaleBase):
    _ALL_TOOLS = {"sparkrun", "docker", "systemctl", "tailscale", "cloudflared",
                  "uv", "python3", "git", "curl", "hf", "gitleaks"}

    def test_report_only_without_yes(self):
        cp = self._setup()
        rt = FakeRuntime(available=self._ALL_TOOLS)
        rc = init_cmd.run(_args(cp, rt, hosts="alpha, beta", yes=False))
        self.assertEqual(rc, 0)
        # report mode: only read-only capability probes (docker info) may run
        self.assertTrue(all(a[:2] == ["docker", "info"] for a in rt.calls))

    def test_yes_bootstrap_refreshes_checkout_and_dirs(self):
        cp = self._setup()
        rt = FakeRuntime(available=self._ALL_TOOLS)
        rc = init_cmd.run(_args(cp, rt, hosts="beta", yes=True))
        self.assertEqual(rc, 0)
        joined = [" ".join(map(str, c)) for c in rt.calls]
        # checkout exists (stub test -d succeeds) and is clean -> refresh issued
        self.assertTrue(any("git -C" in c and "fetch" in c for c in joined))
        self.assertTrue(any("git -C" in c and "pull" in c for c in joined))
        self.assertTrue(any("mkdir -p" in c and str(self.install) in c for c in joined))
        self.assertTrue(any("tailscaled" in c for c in joined))

    def test_bootstrap_missing_checkout_without_repo_url_warns(self):
        cp = self._setup()

        class NoCheckout(FakeRuntime):
            """`test -d <repo>/.git` fails -> the checkout is missing."""
            def run(self, argv):
                argv = [str(x) for x in argv]
                self.calls.append(argv)
                if argv and argv[0] == "sh" and "test -d" in " ".join(argv[1:]):
                    class _R:
                        returncode = 1
                    return _R()
                return super().run(argv)

        rt = NoCheckout(available=self._ALL_TOOLS)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = init_cmd.run(_args(cp, rt, hosts="alpha", yes=True))
        self.assertEqual(rc, 0)
        self.assertIn("repo_url", buf.getvalue())   # the actionable warning


if __name__ == "__main__":
    unittest.main(verbosity=2)
