"""Remote operator mode tests -- all hermetic (no SSH, no real fabric connection).

A :class:`StubConnection` emulates exactly the command grammar
``sparklab.core.remote`` emits (``bash -lc '<PATH prefix>; <cmd>'``), backed by
an in-memory dict of remote files + a dir set. The remote runtime / install FS /
state are exercised end-to-end through it, and the converge plan is asserted to
be **identical** to local mode for the same config (plan parity).

``install_dir`` in the reference config is a fixed absolute path (/opt/sparklab,
no ``~``), so node-side paths are identical in local and remote mode and the
parity assertions hold on every machine. ``SPARKRUN`` is pinned so sparkrun
resolves to the bare name on both sides (the ``~``-expansion path is covered
separately).
"""

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import config as config_mod, converge, node, remote, render, state as state_mod  # noqa: E402
from sparklab.core import runtime as runtime_mod  # noqa: E402
from sparklab.commands import apply as apply_cmd, adopt as adopt_cmd, status as status_cmd, \
    teardown as teardown_cmd, validate as validate_cmd  # noqa: E402
from tests.helpers import FakeRuntime, REFERENCE_CONFIG, REFERENCE_ENV, SECRET_DUMMY  # noqa: E402

STATE_PATH = "/home/user/spark-lab/.sparklab-state/state.json"   # stub home + default repo_dir
INSTALL = "/opt/sparklab"


class _Result:
    def __init__(self, return_code, stdout, stderr=""):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.return_code == 0


class _StubStdin:
    """Paramiko-stdin stand-in: records everything written to it."""

    def __init__(self, sink):
        self.sink = sink

    def write(self, data):
        self.sink.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class _StubStdout:
    """Paramiko-stdout stand-in: readline() yields the canned lines, then b""."""

    def __init__(self, text):
        self._lines = [l.encode("utf-8") for l in text.splitlines(keepends=True)] \
            if text else []

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _StubChannel:
    """Raw-channel stand-in (fabric ``create_session()`` -> paramiko Channel).

    ``exec_command`` dispatches against the stub's ``canned`` table exactly
    like ``StubConnection.run``; written stdin bytes are captured for
    assertion (the sudo password must arrive here, never in the command).
    """

    def __init__(self, stub):
        self.stub = stub
        self.cmds = []
        self.stdin_sink = []
        self.combined_stderr = False
        self._result = _Result(0, "")

    def set_combine_stderr(self, on):
        self.combined_stderr = bool(on)

    def exec_command(self, cmd, *a, **kw):
        self.cmds.append(cmd)
        self.stub.runs.append(cmd)
        self._result = next(
            (_Result(rc, out) for n, rc, out in self.stub.canned if n in cmd),
            _Result(0, ""))
        return (_StubStdin(self.stdin_sink), _StubStdout(self._result.stdout),
                _StubStdout(""))

    def recv_exit_status(self):
        return self._result.return_code


class _StubSFTPFile:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def read(self):
        if self._path not in self._store:
            raise FileNotFoundError(self._path)
        return self._store[self._path]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubSFTPClient:
    """Stands in for the SFTP client that ``conn.open_sftp()`` returns."""

    def __init__(self, store):
        self._store = store

    def open(self, path, mode="rb"):
        return _StubSFTPFile(self._store, str(path))

    def close(self):
        pass


class StubConnection:
    """A fake fabric ``Connection`` emulating remote.py's exact command grammar.

    * ``run`` records every full command string and emulates the shell builtins
      it uses (echo $HOME, command -v, test, cat, rm, mkdir, ls) against
      in-memory ``dirs`` / ``files``; anything else (docker, sparkrun,
      systemctl, the readiness probe) succeeds.
    * ``open`` / ``put`` emulate SFTP reads / uploads.
    * ``canned`` lets individual tests force rc/stdout for specific commands.
    """

    def __init__(self, home="/home/user", binaries=None, canned=None, dirs=None, files=None):
        self.home = home
        self.binaries = dict(binaries or {})
        self.canned = list(canned or [])          # [(substring, rc, stdout)]
        self.dirs = set(dirs or set())
        self.files = dict(files or {})            # remote path (str) -> bytes
        self.runs = []                            # full command strings, in order
        self.puts = []                            # [(local_path, remote_path)]
        self.sessions = []                        # raw channels (create_session)

    @staticmethod
    def _inner(cmd: str) -> str:
        blob = cmd.split("bash -lc ", 1)[1]
        inner = shlex.split(blob)[0]
        if inner.startswith("export PATH="):
            inner = inner.split("; ", 1)[1]
        return inner

    def sftp(self):
        """An SFTP client standing in for fabric's ``conn.sftp()``."""
        return _StubSFTPClient(self.files)

    def create_session(self):
        """A raw channel stand-in for fabric's ``conn.create_session()``."""
        ch = _StubChannel(self)
        self.sessions.append(ch)
        return ch

    def put(self, local, remote_path):
        self.puts.append((str(local), str(remote_path)))
        self.files[str(remote_path)] = Path(local).read_bytes()

    def run(self, cmd, warn=False, **kwargs):
        self.runs.append(cmd)
        for needle, rc, out in self.canned:
            if needle in cmd:
                return _Result(rc, out)
        inner = self._inner(cmd)
        if inner == 'echo "$HOME"':
            return _Result(0, self.home)
        if inner.endswith(" &"):
            return _Result(0, "")                 # detached launch: channel closes immediately
        toks = shlex.split(inner)
        head = toks[0]
        if head == "command" and len(toks) >= 3:
            return _Result(0, self.binaries[toks[2]]) if toks[2] in self.binaries \
                else _Result(1, "")
        if head == "test" and len(toks) >= 3:
            p = toks[2]
            if toks[1] == "-d":
                return _Result(0 if p in self.dirs else 1, "")
            return _Result(0 if p in self.files else 1, "")
        if head == "cat" and len(toks) >= 2:
            p = toks[1]
            if p in self.files:
                return _Result(0, self.files[p].decode("utf-8"))
            return _Result(1, "", f"cat: {p}: No such file")
        if head == "rm" and len(toks) >= 3:
            self.files.pop(toks[2], None)
            return _Result(0, "")
        if head == "mkdir" and len(toks) >= 3:
            self.dirs.add(toks[2])
            return _Result(0, "")
        if head == "ls" and len(toks) >= 2:
            d = toks[1]
            names = [k[len(d) + 1:] for k in self.files if k.startswith(d + "/")]
            names = sorted(n[:-5] for n in names if n.endswith(".yaml"))
            return _Result(0, "\n".join(names))
        return _Result(0, "")                     # docker / sparkrun / systemctl / probe / ...


def make_runtime(stub=None, host="luna", user="user", install_dir="~/AI", repo_dir="~/spark-lab"):
    stub = stub or StubConnection()
    target = remote.RemoteTarget(host=host, user=user, install_dir=install_dir, repo_dir=repo_dir)
    return remote.RemoteRuntime(target, connection=stub), stub


def remote_config_text() -> str:
    """The reference config + an install.remote block (absolute install_dir)."""
    return REFERENCE_CONFIG.replace(
        "install_dir: /opt/sparklab",
        "install_dir: /opt/sparklab\n  remote:\n    host: luna\n",
        1,
    )


class TestShellGrammar(unittest.TestCase):
    def test_run_wraps_in_login_shell_with_path_prefix(self):
        rt, stub = make_runtime()
        rt.run(["docker", "compose", "-f", "/opt/x/y.yml", "up", "-d"])
        self.assertEqual(
            stub.runs[0],
            "bash -lc 'export PATH=\"$HOME/.local/bin:$HOME/.cargo/bin:$PATH\"; "
            "docker compose -f /opt/x/y.yml up -d'",
        )

    def test_run_quotes_argv_elements_with_spaces(self):
        rt, stub = make_runtime()
        rt.run(["sh", "-c", "for i in $(seq 1 120); do echo hi; done"])
        # The whole loop must survive as ONE remote shell word (no local
        # expansion of $(seq)): round-trip the emitted command through the
        # same shell parsing and check the argv is intact.
        inner = StubConnection._inner(stub.runs[0])
        self.assertEqual(shlex.split(inner),
                         ["sh", "-c", "for i in $(seq 1 120); do echo hi; done"])

    def test_spawn_is_detached(self):
        rt, stub = make_runtime()
        p = rt.spawn(["sparkrun", "run", "/opt/sparklab/sparkrun/recipes/qwen.yaml",
                      "--ensure", "--hosts", "127.0.0.1"])
        self.assertIn(
            "setsid nohup sparkrun run /opt/sparklab/sparkrun/recipes/qwen.yaml "
            "--ensure --hosts 127.0.0.1 </dev/null >/dev/null 2>&1 &",
            stub.runs[0],
        )
        self.assertEqual(p.argv[0], "sparkrun")
        self.assertIsNone(p.pid)

    def test_available_and_locate(self):
        stub = StubConnection(binaries={"sparkrun": "/home/user/.local/bin/sparkrun"})
        rt, _ = make_runtime(stub=stub)
        self.assertTrue(rt.available("sparkrun"))
        self.assertEqual(rt.locate("sparkrun"), "/home/user/.local/bin/sparkrun")
        self.assertFalse(rt.available("tailscale"))
        self.assertIsNone(rt.locate("tailscale"))

    def test_home_expansion_is_remote_not_local(self):
        # a stub home different from the operator's proves the remote home wins
        stub = StubConnection(home="/remote/home")
        rt, _ = make_runtime(stub=stub)
        self.assertEqual(rt.expand("~/AI"), "/remote/home/AI")
        self.assertEqual(rt.expand("~"), "/remote/home")
        self.assertEqual(rt.expand("/opt/sparklab"), "/opt/sparklab")


class TestRunSudo(unittest.TestCase):
    """Remote sudo: prompt on the operator's terminal, password over the
    channel (never in the command line), cached/pwdless sudo skips the prompt."""

    def _rt(self, canned):
        rt, _ = make_runtime(stub=StubConnection(canned=canned))
        return rt

    def test_passwordless_or_cached_sudo_skips_prompt(self):
        stub = StubConnection(canned=[("sudo -n -v", 0, "")])
        rt, _ = make_runtime(stub=stub)
        with mock.patch("sparklab.core.remote.getpass.getpass") as gp:
            cp = rt.run_sudo(["sh", "-lc", "apt-get install -y git"])
        self.assertEqual(cp.returncode, 0)
        gp.assert_not_called()
        self.assertTrue(any("sudo -n sh -lc" in r for r in stub.runs))

    def test_password_prompted_and_sent_over_channel_only(self):
        stub = StubConnection(canned=[("sudo -n -v", 1, ""),
                                      ("apt-get", 0, "ok\n")])
        rt, _ = make_runtime(stub=stub)
        with mock.patch("sparklab.core.remote.getpass.getpass",
                        return_value="sekret") as gp, \
             mock.patch.object(sys.stdin, "isatty", return_value=True):
            cp = rt.run_sudo(["sh", "-lc", "apt-get install -y git"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout, "ok\n")
        gp.assert_called_once()
        main = [r for r in stub.runs if "sudo -S -v && sudo -S" in r]
        self.assertEqual(len(main), 1)
        self.assertIn("sudo -S sh -lc", main[0])
        self.assertIn("apt-get install -y git", main[0])
        self.assertEqual(b"".join(stub.sessions[-1].stdin_sink), b"sekret\n")
        # the password must never appear in any remote command string
        self.assertTrue(all("sekret" not in r for r in stub.runs))

    def test_non_interactive_terminal_refuses(self):
        rt = self._rt(canned=[("sudo -n -v", 1, "")])
        with mock.patch("sparklab.core.remote.getpass.getpass") as gp, \
             mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(RuntimeError):
                rt.run_sudo(["sh", "-lc", "x"])
        gp.assert_not_called()


class TestRemoteState(unittest.TestCase):
    def test_roundtrip(self):
        rt, stub = make_runtime()
        st = remote.RemoteState(rt)
        self.assertEqual(st.load(), {"files": {}})
        self.assertIsNone(st.model)
        st.set_state({"a": "h1", "b": "h2"}, {"name": "qwen", "hash": "h"})
        self.assertEqual(st.files, {"a": "h1", "b": "h2"})
        self.assertEqual(st.model, {"name": "qwen", "hash": "h"})
        # exactly the node-side state path (inside the node's checkout)
        self.assertIn(STATE_PATH, stub.files)
        st.clear()
        self.assertEqual(st.load(), {"files": {}})
        self.assertNotIn(STATE_PATH, stub.files)

    def test_corrupt_state_is_treated_as_empty(self):
        stub = StubConnection(files={STATE_PATH: b"not json"})
        rt, _ = make_runtime(stub=stub)
        self.assertEqual(remote.RemoteState(rt).load(), {"files": {}})


class TestRemoteInstallFS(unittest.TestCase):
    def test_write_read_exists_hash_recipes(self):
        rt, stub = make_runtime(install_dir=INSTALL)
        fs = remote.RemoteInstallFS(rt)
        self.assertFalse(fs.base_exists())
        stub.dirs.add(INSTALL)
        self.assertTrue(fs.base_exists())

        self.assertIsNone(fs.read("litellm/config.yaml"))
        fs.write("litellm/config.yaml", b"model_list: []")
        self.assertEqual(fs.read("litellm/config.yaml"), b"model_list: []")
        self.assertTrue(fs.exists("litellm/config.yaml"))

        stub.files[f"{INSTALL}/sparkrun/recipes/b.yaml"] = b"b"
        stub.files[f"{INSTALL}/sparkrun/recipes/a.yaml"] = b"a"
        self.assertEqual(fs.list_recipes(), ["a", "b"])
        hashes = fs.hash_files(["sparkrun/recipes/a.yaml", "missing.yaml"])
        self.assertEqual(hashes["sparkrun/recipes/a.yaml"], state_mod.sha256_bytes(b"a"))
        self.assertIsNone(hashes["missing.yaml"])

    def test_write_creates_parent_dir_and_uploads(self):
        rt, stub = make_runtime(install_dir=INSTALL)
        fs = remote.RemoteInstallFS(rt)
        fs.write("litellm/deep/nested/file.txt", b"x")
        self.assertIn(f"{INSTALL}/litellm/deep/nested", stub.dirs)
        self.assertEqual(stub.files[f"{INSTALL}/litellm/deep/nested/file.txt"], b"x")
        # uploaded from a local temp file (the system tempdir), then cleaned up
        self.assertTrue(any(os.path.basename(l).startswith("sparklab-")
                            for l, _ in stub.puts))

    def test_write_sets_explicit_modes(self):
        """SFTP-created files land 0600; chmod fixes that or the
        prometheus/grafana containers crash-loop on unreadable configs."""
        rt, stub = make_runtime(install_dir=INSTALL)
        fs = remote.RemoteInstallFS(rt)
        fs.write("litellm/.env", b"LITELLM_MASTER_KEY=x")
        fs.write("litellm/prometheus.yml", b"global: {}")
        chmods = [r for r in stub.runs if "chmod" in r]
        self.assertEqual(len(chmods), 2)
        self.assertIn(f"chmod 600 {INSTALL}/litellm/.env", chmods[0])
        self.assertIn(f"chmod 644 {INSTALL}/litellm/prometheus.yml", chmods[1])

    def test_delete_issues_rm(self):
        rt, stub = make_runtime(install_dir=INSTALL)
        fs = remote.RemoteInstallFS(rt)
        fs.delete("sparkrun/recipes/old.yaml")
        self.assertTrue(any(f"rm -f {INSTALL}/sparkrun/recipes/old.yaml" in r
                            for r in stub.runs))


class TestNodeEnv(unittest.TestCase):
    def test_local_write_sets_explicit_modes(self):
        """Local writes must not honor the process umask (077 on the Sparks):"""
        d = Path(tempfile.mkdtemp())
        fs = node.LocalInstallFS(d)
        fs.write("litellm/.env", b"x")
        fs.write("litellm/prometheus.yml", b"y")
        self.assertEqual(d.joinpath("litellm", ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(d.joinpath("litellm", "prometheus.yml").stat().st_mode & 0o777, 0o644)

    def test_local_delete_is_idempotent(self):
        d = Path(tempfile.mkdtemp())
        fs = node.LocalInstallFS(d)
        fs.write("a/b.yaml", b"x")
        fs.delete("a/b.yaml")
        self.assertFalse(d.joinpath("a/b.yaml").exists())
        fs.delete("a/b.yaml")   # missing file is not an error

    def test_local_backend_for_local_runtime(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(REFERENCE_CONFIG)
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        with mock.patch.dict(os.environ, SECRET_DUMMY):
            fs, st = node.node_env(cfg, FakeRuntime())
        self.assertIsInstance(fs, node.LocalInstallFS)
        self.assertIsInstance(st, state_mod.State)

    def test_remote_backend_for_remote_runtime(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(remote_config_text())
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        rt, _ = make_runtime()
        with mock.patch.dict(os.environ, SECRET_DUMMY):
            fs, st = node.node_env(cfg, rt)
        self.assertIsInstance(fs, remote.RemoteInstallFS)
        self.assertIsInstance(st, remote.RemoteState)


class TestRuntimeFor(unittest.TestCase):
    def test_local_config_gets_local_runtime(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(REFERENCE_CONFIG)
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        with mock.patch.dict(os.environ, SECRET_DUMMY):
            rt = runtime_mod.runtime_for(cfg)
        self.assertIsInstance(rt, runtime_mod.Runtime)
        self.assertFalse(rt.is_remote)

    def test_remote_config_gets_remote_runtime_with_lazy_connection(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(remote_config_text())
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        stub = StubConnection()
        with mock.patch.dict(os.environ, SECRET_DUMMY):
            with mock.patch.object(remote, "build_connection", return_value=stub) as bc:
                rt = runtime_mod.runtime_for(cfg)
        self.assertIsInstance(rt, remote.RemoteRuntime)
        self.assertIs(rt.conn, stub)
        bc.assert_called_once()
        self.assertEqual(rt.target.host, "luna")


class TestPlanParity(unittest.TestCase):
    """Local vs remote converge plans must be command-for-command identical."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        self.rendered = None

    def tearDown(self):
        self._env.stop()

    def _load(self, text):
        cfg_path = self.d / "config.yaml"
        cfg_path.write_text(text)
        (self.d / ".env").write_text(REFERENCE_ENV)
        return config_mod.load(str(cfg_path))

    def _plan(self, cfg, runtime, out_name):
        rendered = render.render(cfg, self.d / out_name)
        return converge.build_plan(cfg, rendered, {}, None, allow_restart=True,
                                   runtime=runtime)

    def _commands(self, plan):
        return [[desc, [str(x) for x in argv]] for desc, argv in plan.commands]

    def test_local_and_remote_plans_match(self):
        cfg_local = self._load(REFERENCE_CONFIG)
        plan_local = self._plan(cfg_local, FakeRuntime(), "dep-local")

        cfg_remote = self._load(remote_config_text())
        self.assertTrue(cfg_remote.is_remote)
        rt, stub = make_runtime()
        plan_remote = self._plan(cfg_remote, rt, "dep-remote")

        self.assertEqual(self._commands(plan_remote), self._commands(plan_local))
        # the remote home was fetched (for node-side path resolution) before planning
        self.assertTrue(any('echo "$HOME"' in r for r in stub.runs[:1]))

    def test_remote_tilde_install_dir_becomes_absolute_node_path(self):
        text = remote_config_text().replace("install_dir: /opt/sparklab",
                                           "install_dir: ~/AI", 1)
        cfg = self._load(text)
        rt, _ = make_runtime()
        plan = self._plan(cfg, rt, "dep-tilde")
        descs = {desc for desc, _ in plan.commands}
        compose_cmd = [argv for desc, argv in plan.commands
                       if desc == "Reconcile LiteLLM + monitoring stack (up + remove orphans)"][0]
        self.assertEqual(compose_cmd[3], "/home/user/AI/litellm/docker-compose.yml")
        self.assertIn("Start/ensure model workload (detached)", descs)


class TestApplyRemoteEndToEnd(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        (self.d / "config.yaml").write_text(remote_config_text())
        (self.d / ".env").write_text(REFERENCE_ENV)
        self.stub = StubConnection()
        self.rt, _ = make_runtime(stub=self.stub, install_dir=INSTALL)
        self.args = SimpleNamespace(config=str(self.d / "config.yaml"), dry_run=False,
                                    apply=True, yes=False, diff=False, runtime=self.rt)

    def tearDown(self):
        self._env.stop()

    def test_apply_pushes_files_state_and_converges_remotely(self):
        rc = apply_cmd.run(self.args)
        self.assertEqual(rc, 0)

        # 1. every rendered file was pushed to the node install dir
        cfg = config_mod.load(self.args.config)
        rendered = render.render(cfg, self.d / "probe-deploy")
        pushed = {r for _, r in self.stub.puts}
        for rel in rendered:
            self.assertIn(f"{INSTALL}/{rel}", pushed)
        for rel in rendered:
            self.assertEqual(self.stub.files[f"{INSTALL}/{rel}"], rendered[rel])

        # 2. the converge commands ran on the node, in order:
        #    control plane up -> detached model launch -> bounded probe -> tailscale
        joined = "\n".join(self.stub.runs)
        i_up = joined.index("docker compose -f /opt/sparklab/litellm/docker-compose.yml up -d")
        # the launch is a sh -c wrapper (records its PID for the probe's crash
        # detection) exec'ing the ensure, detached with the launch log captured
        i_spawn = joined.index(
            "echo $$ > /tmp/sparklab-model-launch.pid; exec sparkrun run "
            "/opt/sparklab/sparkrun/recipes/qwen.yaml --ensure")
        self.assertIn(
            ">/tmp/sparklab-model-launch.log 2>&1 &", joined[i_spawn:i_spawn + 400])
        i_probe = joined.index("for i in $(seq 1 120)")
        i_ts = joined.index("systemctl enable --now tailscaled")
        self.assertTrue(i_up < i_spawn < i_probe < i_ts)

        # 3. state was recorded ON THE NODE (its checkout), with the model confirmed
        data = json.loads(self.stub.files[STATE_PATH])
        self.assertEqual(set(data["files"]), set(rendered))
        self.assertEqual(data["model"], {"name": "qwen",
                                         "hash": rendered_hash_of(rendered)})

    def test_second_apply_is_a_clean_noop(self):
        self.assertEqual(apply_cmd.run(self.args), 0)
        self.assertEqual(apply_cmd.run(self.args), 0)
        # no compose up on the second apply (litellm untouched), but the model
        # --ensure + probe always run (idempotent convergence of the workload)
        ups = [r for r in self.stub.runs if "compose -f /opt/sparklab" in r and " up -d" in r]
        self.assertEqual(len(ups), 1)


def rendered_hash_of(rendered: dict) -> str:
    return state_mod.sha256_bytes(rendered[f"sparkrun/recipes/qwen.yaml"])


class TestAdoptRemote(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        (self.d / "config.yaml").write_text(remote_config_text())
        (self.d / ".env").write_text(REFERENCE_ENV)
        self.cfg = config_mod.load(str(self.d / "config.yaml"))
        rendered = render.render(self.cfg, self.d / "deploy")
        files = {f"{INSTALL}/{rel}": data for rel, data in rendered.items()}
        self.stub = StubConnection(dirs={INSTALL, "/home/user/spark-lab"}, files=files)
        self.rt, _ = make_runtime(stub=self.stub, install_dir=INSTALL)

    def tearDown(self):
        self._env.stop()

    def test_adopt_records_on_node_reality_and_writes_state_on_node(self):
        args = SimpleNamespace(config=str(self.d / "config.yaml"), dry_run=False,
                               runtime=self.rt)
        rc = adopt_cmd.run(args)
        self.assertEqual(rc, 0)
        data = json.loads(self.stub.files[STATE_PATH])
        rendered = render.render(self.cfg, self.d / "probe")
        self.assertEqual(set(data["files"]), set(rendered))
        self.assertEqual(data["model"]["name"], "qwen")
        # nothing was written into the install dir by adoption
        puts_to_install = [r for _, r in self.stub.puts if r.startswith(INSTALL)]
        self.assertEqual(puts_to_install, [])

    def test_adopt_is_idempotent(self):
        args = SimpleNamespace(config=str(self.d / "config.yaml"), dry_run=False,
                               runtime=self.rt)
        self.assertEqual(adopt_cmd.run(args), 0)
        self.assertEqual(adopt_cmd.run(args), 0)


class TestTeardownStatusValidateRemote(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()
        self.d = Path(tempfile.mkdtemp())
        (self.d / "config.yaml").write_text(remote_config_text())
        (self.d / ".env").write_text(REFERENCE_ENV)

    def tearDown(self):
        self._env.stop()

    def test_teardown_stops_composes_down_and_clears_remote_state(self):
        stub = StubConnection(
            binaries={"sparkrun": "/home/user/.local/bin/sparkrun",
                      "docker": "/usr/bin/docker"},
            files={STATE_PATH: b'{"files": {"a": "h"}}'})
        rt, _ = make_runtime(stub=stub)
        args = SimpleNamespace(config=str(self.d / "config.yaml"), yes=True, purge=False,
                               runtime=rt)
        self.assertEqual(teardown_cmd.run(args), 0)
        joined = "\n".join(stub.runs)
        self.assertIn("sparkrun stop /opt/sparklab/sparkrun/recipes/qwen.yaml --hosts 127.0.0.1",
                      joined)
        self.assertIn("docker compose -f /opt/sparklab/litellm/docker-compose.yml down", joined)
        self.assertNotIn(STATE_PATH, stub.files)

    def test_status_runs_on_the_node(self):
        stub = StubConnection(
            binaries={"sparkrun": "/home/user/.local/bin/sparkrun",
                      "docker": "/usr/bin/docker",
                      "tailscale": "/usr/bin/tailscale"})
        rt, _ = make_runtime(stub=stub)
        args = SimpleNamespace(config=str(self.d / "config.yaml"), runtime=rt)
        self.assertEqual(status_cmd.run(args), 0)
        joined = "\n".join(stub.runs)
        self.assertIn("sparkrun status", joined)
        self.assertIn("docker compose -f /opt/sparklab/litellm/docker-compose.yml ps", joined)
        self.assertIn("tailscale status", joined)

    def test_validate_preflight_checks_the_remote_binaries(self):
        stub = StubConnection(binaries={"sparkrun": "/home/user/.local/bin/sparkrun",
                                        "docker": "/usr/bin/docker",
                                        "tailscale": "/usr/bin/tailscale"})
        rt, _ = make_runtime(stub=stub)
        args = SimpleNamespace(config=str(self.d / "config.yaml"), verbose=False,
                               json=False, runtime=rt)
        self.assertEqual(validate_cmd.run(args), 0)
        # pre-flight queried the node, not the local machine
        self.assertTrue(any("command -v sparkrun" in r for r in stub.runs))


class TestFindSparkrunRemote(unittest.TestCase):
    def test_locates_on_the_node(self):
        stub = StubConnection(binaries={"sparkrun": "/home/user/.local/bin/sparkrun"})
        rt, _ = make_runtime(stub=stub)
        self.assertEqual(converge.find_sparkrun(rt), "/home/user/.local/bin/sparkrun")

    def test_falls_back_to_bare_name(self):
        rt, _ = make_runtime()
        self.assertEqual(converge.find_sparkrun(rt), "sparkrun")

    def test_sparkrun_env_var_wins_everywhere(self):
        with mock.patch.dict(os.environ, {"SPARKRUN": "/opt/special/sparkrun"}):
            self.assertEqual(converge.find_sparkrun(), "/opt/special/sparkrun")
            rt, _ = make_runtime()
            self.assertEqual(converge.find_sparkrun(rt), "/opt/special/sparkrun")


if __name__ == "__main__":
    unittest.main(verbosity=2)
