"""Tests for `spark-lab model stop` — targeted model stop (stack stays up).

Covers the gate (no --yes => refuse, nothing runs), the sparkrun stop argv,
the state update (model entry cleared, file hashes untouched), and the remote
path (stop on the node, state written on the node).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import config as config_mod, remote, state as state_mod  # noqa: E402
from sparklab.commands import model as model_cmd  # noqa: E402
from tests.helpers import FakeRuntime, REFERENCE_ENV, SECRET_DUMMY, config_text  # noqa: E402
from tests.test_remote import STATE_PATH, StubConnection, make_runtime  # noqa: E402


def make_args(tmp: Path, yes: bool, runtime):
    return SimpleNamespace(config=str(tmp / "config.yaml"), yes=yes, runtime=runtime)


class TestModelStopLocal(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        (self.d / "config.yaml").write_text(config_text(str(self.d / "install")))
        (self.d / ".env").write_text(REFERENCE_ENV)
        self.cfg = config_mod.load(str(self.d / "config.yaml"))
        files = {"litellm/docker-compose.yml": "h1", "sparkrun/recipes/qwen.yaml": "h2"}
        state_mod.State(self.cfg.state_dir).set_state(files,
                                                      {"name": "qwen", "hash": "h2"})

    def tearDown(self):
        self._env.stop()

    def test_refuses_without_yes_and_runs_nothing(self):
        rt = FakeRuntime()
        rc = model_cmd.stop(make_args(self.d, yes=False, runtime=rt))
        self.assertEqual(rc, 1)
        self.assertEqual(rt.calls, [])
        st = state_mod.State(self.cfg.state_dir)
        self.assertEqual(st.model, {"name": "qwen", "hash": "h2"})

    def test_stop_runs_sparkrun_stop_and_clears_model_entry(self):
        rt = FakeRuntime()
        rc = model_cmd.stop(make_args(self.d, yes=True, runtime=rt))
        self.assertEqual(rc, 0)
        self.assertEqual(
            rt.calls[0],
            ["sparkrun", "stop", str(self.d / "install" / "sparkrun" / "recipes" / "qwen.yaml"),
             "--hosts", "127.0.0.1"],
        )
        st = state_mod.State(self.cfg.state_dir)
        self.assertIsNone(st.model)
        # file hashes untouched
        self.assertEqual(st.files, {"litellm/docker-compose.yml": "h1",
                                     "sparkrun/recipes/qwen.yaml": "h2"})


class TestModelStopRemote(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        text = config_text("/opt/sparklab").replace(
            "install_dir: /opt/sparklab",
            "install_dir: /opt/sparklab\n  remote:\n    host: luna\n", 1)
        (self.d / "config.yaml").write_text(text)
        (self.d / ".env").write_text(REFERENCE_ENV)
        state = json.dumps({"files": {"sparkrun/recipes/qwen.yaml": "h2"},
                            "model": {"name": "qwen", "hash": "h2"}}, sort_keys=True) + "\n"
        self.stub = StubConnection(
            binaries={"sparkrun": "/home/user/.local/bin/sparkrun"},
            files={STATE_PATH: state.encode()})
        self.rt, _ = make_runtime(stub=self.stub, install_dir="/opt/sparklab")

    def tearDown(self):
        self._env.stop()

    def test_stop_runs_on_the_node_and_clears_state_there(self):
        rc = model_cmd.stop(make_args(self.d, yes=True, runtime=self.rt))
        self.assertEqual(rc, 0)
        joined = "\n".join(self.stub.runs)
        self.assertIn("sparkrun stop /opt/sparklab/sparkrun/recipes/qwen.yaml --hosts 127.0.0.1",
                      joined)
        # state on the node: model entry gone, file hashes kept
        data = json.loads(self.stub.files[STATE_PATH])
        self.assertNotIn("model", data)
        self.assertEqual(data["files"], {"sparkrun/recipes/qwen.yaml": "h2"})

    def test_refuses_without_yes(self):
        rt = self.rt
        rc = model_cmd.stop(make_args(self.d, yes=False, runtime=rt))
        self.assertEqual(rc, 1)
        self.assertEqual(self.stub.runs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
