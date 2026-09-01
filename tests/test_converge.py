"""Unit tests for the converge logic (no node, no subprocess, no real config).

These pin down the "stable to update" contract:
  * no-op stays a no-op (no model restart)
  * add / remove / switch a recipe converges (stop the old, start the new)
  * a recipe change that is not restarted stays *pending* (does not drift)
  * the recorded state tracks which recipe the model is actually running

Run with:  python tests/test_converge.py
"""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# make `lib` importable when run as a plain script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparklab.core import converge, state  # noqa: E402
from sparklab.core import node as node_mod  # noqa: E402
from tests.helpers import FakeRuntime  # noqa: E402


def make_cfg(recipe="mymodel", cluster=False):
    c = types.SimpleNamespace()
    c.recipe_name = recipe
    c.install_dir = "/tmp/sparklab-test"
    c.is_cluster = cluster
    c.cluster_name = "spark"
    c.hosts = ["10.0.0.1", "10.0.0.2"] if cluster else []
    c.model = {"port": 30000}      # active model definition (probe reads .port)
    c.active_alias = recipe
    c.model_host_list = lambda alias: []   # spanning placement (empty = single-node)
    c.tailscale = lambda: {"enabled": False}
    c.cloudflare = lambda: {"enabled": False}
    c.prometheus = lambda: {"port": 9090}
    c.litellm = {"port": 4000}   # gateway health probe reads .port
    c.monitoring_role = lambda: "full"
    c.control_plane_enabled = lambda: True
    return c


def recipe_bytes(recipe="mymodel", content="name: mymodel\n"):
    return {"sparkrun/recipes/%s.yaml" % recipe: content.encode("utf-8")}


LIT = {"litellm/docker-compose.yml": b"services: {}\n"}


def plan_for(cfg, rendered, files, model, allow_restart):
    return converge.build_plan(cfg, rendered, files, model, allow_restart)


def converged_after(plan, allow_restart):
    """Mirror cmd_apply: the model is converged after this apply iff no gated
    stop was left pending (or, for a removal, the stop was allowed)."""
    has_model = plan.current_hash is not None
    return (not plan.model_restart_pending) if has_model else allow_restart


class TestConverge(unittest.TestCase):
    def setUp(self):
        # Never touch the real sparkrun during these tests (scoped to this class
        # so the real find_sparkrun is restored for later test modules).
        self._p = mock.patch.object(converge, "find_sparkrun", lambda *a: "sparkrun")
        self._p.start()

    # 0. Readiness probe bound -------------------------------------------------
    def test_readiness_probe_defaults_to_six_hundred_seconds(self):
        loop = converge._model_readiness_probe(make_cfg())[2]
        self.assertIn("seq 1 120", loop)      # 120 x 5s = 600s
        self.assertIn("sleep 5", loop)

    def test_readiness_probe_honors_readiness_seconds(self):
        cfg = make_cfg()
        cfg.model["readiness_seconds"] = 5400   # e.g. a 48 GB PLE-table fill
        loop = converge._model_readiness_probe(cfg)[2]
        self.assertIn("seq 1 1080", loop)     # 5400 / 5
        self.assertIn("5400s", loop)

    def tearDown(self):
        self._p.stop()

    # 0b. Spanning models (min_nodes > 1): head-only launch ------------------
    def test_spanning_head_launches_with_placement_hosts(self):
        cfg = make_cfg()
        cfg.model = {"port": 8888, "min_nodes": 2}
        cfg.active_alias = "mymodel"
        cfg.model_host_list = lambda alias: ["host-a", "host-b"]
        rendered = {**recipe_bytes(), **LIT}
        plan = converge.build_plan(cfg, rendered, {}, None, allow_restart=True,
                                  launch_model=True)
        descs = [d for d, _ in plan.commands]
        # head builds the SSH mesh across the placement
        self.assertTrue(any("SSH mesh" in d for d in descs))
        mesh = [a for d, a in plan.commands if "SSH mesh" in d][0]
        self.assertIn("host-a,host-b", " ".join(map(str, mesh)))
        # run targets the placement via --hosts (not a saved --cluster)
        run_argv = [a for d, a in plan.commands if d.startswith("Start/ensure model")][0]
        run_str = " ".join(map(str, run_argv))
        self.assertIn("--hosts", run_str)
        self.assertIn("host-a,host-b", run_str)
        self.assertNotIn("--cluster", run_str)
        # bounded readiness probe targets the head's local model port
        probe = [a for d, a in plan.commands if d.startswith("Wait for model")][0]
        self.assertIn("8888", " ".join(map(str, probe)))

    def test_spanning_worker_skips_launch(self):
        cfg = make_cfg()
        cfg.model = {"port": 8888, "min_nodes": 2}
        cfg.active_alias = "mymodel"
        cfg.model_host_list = lambda alias: ["host-a", "host-b"]
        rendered = {**recipe_bytes(), **LIT}
        plan = converge.build_plan(cfg, rendered, {}, None, allow_restart=True,
                                  launch_model=False)
        descs = [d for d, _ in plan.commands]
        # worker launches nothing: no run, no mesh, no probe
        self.assertFalse(any(d.startswith("Start/ensure model") for d in descs))
        self.assertFalse(any("SSH mesh" in d for d in descs))
        self.assertFalse(any(d.startswith("Wait for model") for d in descs))
        # treated as converged (no pending restart left behind)
        self.assertTrue(plan.model_converged)
        self.assertFalse(plan.model_restart_pending)
        # and a note explains the worker role
        self.assertTrue(any("Worker host" in n for n in plan.notes))

    # 1. No-op ---------------------------------------------------------------
    def test_noop_is_idempotent(self):
        cfg = make_cfg()
        rendered = {**recipe_bytes(), **LIT}
        files = converge.compute_files_after_apply(rendered)
        model = {"name": "mymodel", "hash": state.sha256_bytes(rendered["sparkrun/recipes/mymodel.yaml"])}
        plan = plan_for(cfg, rendered, files, model, allow_restart=False)
        self.assertEqual(plan.file_changes, [])
        self.assertTrue(plan.model_converged)
        self.assertFalse(plan.model_restart_pending)
        self.assertFalse(any("Stop model" in d for d, _ in plan.commands))
        # recorded state is unchanged
        self.assertEqual(converge.compute_files_after_apply(rendered), files)
        self.assertEqual(converge.compute_model_after_apply(model, "mymodel", model["hash"], True), model)

    # 2b. Fresh model: starts without restart flags, records state ----------
    def test_fresh_model_starts_and_records_state(self):
        cfg = make_cfg()
        rendered = {**recipe_bytes(), **LIT}
        plan = plan_for(cfg, rendered, files={}, model=None, allow_restart=False)
        # brand-new model: nothing running under it, so no stop -> not pending
        self.assertFalse(plan.model_restart_pending)
        self.assertTrue(any("mymodel.yaml" in " ".join(map(str, a)) and "--ensure" in " ".join(map(str, a))
                            for d, a in plan.commands))
        # state now records the running model, so a later content change is caught
        new_model = converge.compute_model_after_apply(
            None, "mymodel", plan.current_hash, converged_after(plan, False))
        self.assertEqual(new_model, {"name": "mymodel", "hash": plan.current_hash})

    # 2c. Changed prometheus.yml triggers a hot reload, not a recreate --------
    def test_changed_prometheus_yml_triggers_hot_reload(self):
        cfg = make_cfg()
        old = {**recipe_bytes(), **LIT, "litellm/prometheus.yml": b"old: 1\n"}
        new = {**recipe_bytes(), **LIT, "litellm/prometheus.yml": b"old: 2\n"}
        files = converge.compute_files_after_apply(old)
        model = {"name": "mymodel",
                 "hash": state.sha256_bytes(old["sparkrun/recipes/mymodel.yaml"])}
        plan = plan_for(cfg, new, files, model, allow_restart=False)
        descs = [d for d, _ in plan.commands]
        self.assertIn("Reload prometheus config (hot, best-effort)", descs)
        # it is best-effort: a first-boot stack has no daemon yet
        self.assertIn("Reload prometheus config (hot, best-effort)", plan.best_effort)
        # and the reload targets the configured prometheus port
        reload_argv = next(a for d, a in plan.commands if d.startswith("Reload prometheus"))
        self.assertIn("http://127.0.0.1:9090/-/reload", " ".join(map(str, reload_argv)))

    def test_unchanged_prometheus_yml_does_not_reload(self):
        cfg = make_cfg()
        rendered = {**recipe_bytes(), **LIT, "litellm/prometheus.yml": b"same: 1\n"}
        files = converge.compute_files_after_apply(rendered)
        model = {"name": "mymodel",
                 "hash": state.sha256_bytes(rendered["sparkrun/recipes/mymodel.yaml"])}
        plan = plan_for(cfg, rendered, files, model, allow_restart=False)
        self.assertFalse(any(d.startswith("Reload prometheus") for d, _ in plan.commands))

    def test_exporters_role_host_skips_reload(self):
        cfg = make_cfg()
        cfg.monitoring_role = lambda: "exporters"
        old = {**recipe_bytes(), **LIT, "litellm/prometheus.yml": b"old: 1\n"}
        new = {**recipe_bytes(), **LIT, "litellm/prometheus.yml": b"old: 2\n"}
        files = converge.compute_files_after_apply(old)
        model = {"name": "mymodel",
                 "hash": state.sha256_bytes(old["sparkrun/recipes/mymodel.yaml"])}
        plan = plan_for(cfg, new, files, model, allow_restart=False)
        self.assertFalse(any(d.startswith("Reload prometheus") for d, _ in plan.commands))

    # 2d. Changed gateway model-list restarts the running litellm -----------
    def _gateway_plan(self, old_litellm_cfg, new_litellm_cfg, cfg=None):
        cfg = cfg or make_cfg()
        old = {**recipe_bytes(), **LIT, "litellm/model_config.yaml": old_litellm_cfg,
               "litellm/config.yaml": b"gateway: 1\n"}
        new = {**recipe_bytes(), **LIT, "litellm/model_config.yaml": new_litellm_cfg,
               "litellm/config.yaml": b"gateway: 1\n"}
        files = converge.compute_files_after_apply(old)
        model = {"name": "mymodel",
                 "hash": state.sha256_bytes(old["sparkrun/recipes/mymodel.yaml"])}
        return plan_for(cfg, new, files, model, allow_restart=False)

    def test_changed_model_config_triggers_litellm_restart(self):
        plan = self._gateway_plan(b"model_list: [a]\n", b"model_list: [b]\n")
        descs = [d for d, _ in plan.commands]
        self.assertIn("Restart litellm to apply the changed model list (best-effort)", descs)
        self.assertIn("Restart litellm to apply the changed model list (best-effort)",
                      plan.best_effort)
        restart_argv = next(a for d, a in plan.commands if d.startswith("Restart litellm"))
        self.assertEqual(restart_argv[-2:], ["restart", "litellm"])

    def test_changed_litellm_gateway_config_triggers_restart(self):
        old = {**recipe_bytes(), **LIT, "litellm/model_config.yaml": b"same\n",
               "litellm/config.yaml": b"gateway: 1\n"}
        new = {**recipe_bytes(), **LIT, "litellm/model_config.yaml": b"same\n",
               "litellm/config.yaml": b"gateway: 2\n"}
        files = converge.compute_files_after_apply(old)
        model = {"name": "mymodel",
                 "hash": state.sha256_bytes(old["sparkrun/recipes/mymodel.yaml"])}
        plan = plan_for(make_cfg(), new, files, model, allow_restart=False)
        self.assertTrue(any(d.startswith("Restart litellm") for d, _ in plan.commands))

    def test_restart_is_followed_by_bounded_health_check(self):
        plan = self._gateway_plan(b"model_list: [a]\n", b"model_list: [b]\n")
        descs = [d for d, _ in plan.commands]
        i = descs.index("Restart litellm to apply the changed model list (best-effort)")
        self.assertEqual(descs[i + 1], "Verify the gateway came back healthy (bounded)")
        self.assertNotIn(descs[i + 1], plan.best_effort)   # failure fails the converge
        argv = dict(plan.commands)[descs[i + 1]]
        self.assertIn("/health/liveliness", " ".join(argv))
        self.assertIn("127.0.0.1:4000", " ".join(argv))

    def test_extra_models_change_or_removal_triggers_restart(self):
        old = {**recipe_bytes(), **LIT, "litellm/model_config.yaml": b"same\n",
               "litellm/config.yaml": b"gateway: 1\n",
               "litellm/extra_models.yaml": b"model_list: [x]\n"}
        files = converge.compute_files_after_apply(old)
        model = {"name": "mymodel",
                 "hash": state.sha256_bytes(old["sparkrun/recipes/mymodel.yaml"])}
        # a changed externally-run-model list restarts the gateway ...
        new = {**old, "litellm/extra_models.yaml": b"model_list: [x, y]\n"}
        plan = plan_for(make_cfg(), new, files, model, allow_restart=False)
        self.assertTrue(any(d.startswith("Restart litellm") for d, _ in plan.commands))
        # ... and so does the file DISAPPEARING (last extra model removed:
        # the running gateway would otherwise keep serving it from its DB).
        new2 = {k: v for k, v in old.items() if k != "litellm/extra_models.yaml"}
        plan2 = plan_for(make_cfg(), new2, files, model, allow_restart=False)
        self.assertTrue(any(d.startswith("Restart litellm") for d, _ in plan2.commands))

    def test_fresh_gateway_files_do_not_restart(self):
        # first apply: the gateway booted from the new files already
        cfg = make_cfg()
        plan = plan_for(cfg, {**recipe_bytes(), **LIT,
                              "litellm/model_config.yaml": b"model_list: [a]\n",
                              "litellm/config.yaml": b"gateway: 1\n"},
                        files={}, model=None, allow_restart=False)
        self.assertFalse(any(d.startswith("Restart litellm") for d, _ in plan.commands))

    def test_control_plane_off_host_never_restarts_litellm(self):
        cfg = make_cfg()
        cfg.control_plane_enabled = lambda: False
        plan = self._gateway_plan(b"model_list: [a]\n", b"model_list: [b]\n", cfg=cfg)
        self.assertFalse(any(d.startswith("Restart litellm") for d, _ in plan.commands))

    # 2. Recipe content change, not restarted -> stays pending ---------------
    def test_recipe_change_stays_pending_without_flag(self):
        cfg = make_cfg()
        rendered = {**recipe_bytes(content="name: mymodel\nparams: {x: 1}\n"), **LIT}
        old = state.sha256_bytes(b"name: mymodel\n")
        files = {
            "sparkrun/recipes/mymodel.yaml": old,
            "litellm/docker-compose.yml": state.sha256_bytes(LIT["litellm/docker-compose.yml"]),
        }
        model = {"name": "mymodel", "hash": old}
        plan = plan_for(cfg, rendered, files, model, allow_restart=False)
        self.assertFalse(plan.model_converged)
        self.assertTrue(plan.model_restart_pending)
        # no stop command issued when restart not requested
        self.assertFalse(any("Stop model" in d for d, _ in plan.commands))
        self.assertTrue(any("--restart-model" in n for n in plan.notes))
        # state keeps the OLD model hash so the next apply still sees the change
        new_model = converge.compute_model_after_apply(model, "mymodel", plan.current_hash, converged_after(plan, False))
        self.assertEqual(new_model, model)

    # 3. Recipe content change, restarted -> converges ----------------------
    def test_recipe_change_converges_with_flag(self):
        cfg = make_cfg()
        rendered = {**recipe_bytes(content="name: mymodel\nparams: {x: 1}\n"), **LIT}
        old = state.sha256_bytes(b"name: mymodel\n")
        files = {"sparkrun/recipes/mymodel.yaml": old}
        model = {"name": "mymodel", "hash": old}
        plan = plan_for(cfg, rendered, files, model, allow_restart=True)
        self.assertTrue(any("Stop model" in d for d, _ in plan.commands))
        self.assertFalse(plan.model_restart_pending)
        new_model = converge.compute_model_after_apply(model, "mymodel", plan.current_hash, converged_after(plan, True))
        self.assertEqual(new_model, {"name": "mymodel", "hash": plan.current_hash})

    # 4. Switch recipe A -> B ------------------------------------------------
    def test_switch_recipe_stops_old_starts_new(self):
        cfg = make_cfg(recipe="llama")
        rendered = {**recipe_bytes("llama", "name: llama\n"), **LIT}
        files = {"sparkrun/recipes/qwen.yaml": "x" * 64}
        model = {"name": "qwen", "hash": "x" * 64}
        plan = plan_for(cfg, rendered, files, model, allow_restart=True)
        stops = [d for d, _ in plan.commands if d.startswith("Stop model")]
        self.assertEqual(stops, ["Stop model workload qwen"])
        self.assertTrue(any("llama.yaml" in " ".join(map(str, a)) and "--ensure" in " ".join(map(str, a))
                            for d, a in plan.commands))
        self.assertFalse(plan.model_restart_pending)
        self.assertEqual(converge.compute_model_after_apply(model, "llama", plan.current_hash, converged_after(plan, True)),
                         {"name": "llama", "hash": plan.current_hash})

    def test_switch_recipe_pending_without_flag(self):
        cfg = make_cfg(recipe="llama")
        rendered = {**recipe_bytes("llama", "name: llama\n")}
        files = {"sparkrun/recipes/qwen.yaml": "x" * 64}
        model = {"name": "qwen", "hash": "x" * 64}
        plan = plan_for(cfg, rendered, files, model, allow_restart=False)
        self.assertTrue(plan.model_restart_pending)
        self.assertFalse(any("Stop model" in d for d, _ in plan.commands))
        self.assertEqual(converge.compute_model_after_apply(model, "llama", plan.current_hash, converged_after(plan, False)),
                         model)  # stays pending

    # 5. Removal (model no longer in config) --------------------------------
    def test_removal_converges_with_flag(self):
        cfg = make_cfg(recipe=None)   # model section removed from config
        rendered = dict(LIT)
        files = {"sparkrun/recipes/qwen.yaml": "y" * 64, "litellm/docker-compose.yml": "y" * 64}
        model = {"name": "qwen", "hash": "y" * 64}
        plan = plan_for(cfg, rendered, files, model, allow_restart=True)
        # scaled-down recipe: kept on disk, unmanaged (not deleted)
        self.assertIn(("sparkrun/recipes/qwen.yaml", "kept"), plan.file_changes)
        self.assertTrue(any("Stop model workload qwen" in d for d, _ in plan.commands))
        self.assertFalse(plan.model_restart_pending)
        # model converges to "none"
        self.assertIsNone(converge.compute_model_after_apply(model, None, None, converged_after(plan, True)))

    def test_removal_pending_without_flag(self):
        cfg = make_cfg(recipe=None)
        rendered = dict(LIT)
        files = {"sparkrun/recipes/qwen.yaml": "y" * 64}
        model = {"name": "qwen", "hash": "y" * 64}
        plan = plan_for(cfg, rendered, files, model, allow_restart=False)
        self.assertTrue(plan.model_restart_pending)
        self.assertFalse(any("Stop model" in d for d, _ in plan.commands))
        self.assertEqual(converge.compute_model_after_apply(model, None, None, converged_after(plan, False)), model)

    # 6. File removal prunes state -------------------------------------------
    def test_file_removal_prunes_state(self):
        rendered = {"sparkrun/recipes/mymodel.yaml": b"name: mymodel\n"}
        files = {
            "sparkrun/recipes/mymodel.yaml": state.sha256_bytes(b"name: mymodel\n"),
            "litellm/old-dash.json": "z" * 64,
        }
        new_files = converge.compute_files_after_apply(rendered)
        self.assertNotIn("litellm/old-dash.json", new_files)
        self.assertIn("sparkrun/recipes/mymodel.yaml", new_files)

    def test_best_effort_failure_does_not_abort(self):
        # A root-gated infra-ensure (systemctl) failing should warn + continue,
        # not abort the converge (the model + stack are the critical commands).
        plan = converge.Plan()
        plan.commands = [
            ("best-effort ensure", ["sysadm", "enable"]),
            ("critical step", ["important", "run"]),
        ]
        plan.best_effort = {"best-effort ensure"}
        rt = FakeRuntime(available={"sysadm", "important"}, fail={"sysadm": 1})
        rc = converge.execute(plan, dry_run=False, runtime=rt, verbose=False)
        self.assertEqual(rc, 0)                      # the best-effort failure is non-fatal
        self.assertTrue(any(a[0] == "important" for a in rt.commands))

    def test_critical_failure_still_aborts(self):
        plan = converge.Plan()
        plan.commands = [
            ("critical step", ["important", "run"]),
            ("second", ["later", "cmd"]),
        ]
        rt = FakeRuntime(available={"important", "later"}, fail={"important": 1})
        rc = converge.execute(plan, dry_run=False, runtime=rt, verbose=False)
        self.assertEqual(rc, 1)
        self.assertEqual(len(rt.commands), 1)         # broke on the critical failure

    # 7. Model launch is detached + a bounded probe follows -------------------
    def test_model_launch_backgrounded_with_probe(self):
        # A fresh single-node apply: the control plane comes up FIRST (gateway
        # available while the model loads), the model is launched detached, and a
        # bounded readiness probe follows it.
        cfg = make_cfg(recipe="mymodel")
        rendered = {**recipe_bytes("mymodel"), **LIT}
        plan = plan_for(cfg, rendered, files={}, model=None, allow_restart=True)
        descs = [d for d, _ in plan.commands]
        self.assertIn("Reconcile LiteLLM + monitoring stack (up + remove orphans)", descs)
        self.assertIn("Start/ensure model workload (detached)", descs)
        self.assertIn("Wait for model to be ready (bounded)", descs)
        self.assertLess(descs.index("Reconcile LiteLLM + monitoring stack (up + remove orphans)"),
                        descs.index("Start/ensure model workload (detached)"))
        self.assertLess(descs.index("Start/ensure model workload (detached)"),
                        descs.index("Wait for model to be ready (bounded)"))
        # the model launch is the detached command; the probe is run inline
        self.assertIn("Start/ensure model workload (detached)", plan.background)
        self.assertNotIn("Wait for model to be ready (bounded)", plan.background)
        # the probe targets the model's /health (port 30000) on a real host
        probe_argv = dict(plan.commands)["Wait for model to be ready (bounded)"]
        self.assertEqual(probe_argv[:2], ["sh", "-c"])
        self.assertIn("30000/health", probe_argv[2])

    def test_execute_spawns_background_commands(self):
        # execute() must launch background commands via spawn (detached), not the
        # blocking run, so the converge doesn't hang on the model's log-tail.
        plan = converge.Plan()
        plan.commands = [
            ("detached model", ["sparkrun", "run", "r.yaml", "--ensure", "--hosts", "x"]),
            ("probe", ["sh", "-c", "curl ..."])
        ]
        plan.background = {"detached model"}
        rt = FakeRuntime(available={"sparkrun", "sh"})
        rc = converge.execute(plan, dry_run=False, runtime=rt, verbose=False)
        self.assertEqual(rc, 0)
        self.assertEqual(rt.spawned,
                         [["sparkrun", "run", "r.yaml", "--ensure", "--hosts", "x"]])
        self.assertEqual(len(rt.commands), 2)         # both still recorded


class TestExpandInstallDir(unittest.TestCase):
    """`{install_dir}` in node-side recipes expands to the node's real install
    dir at write time (per host), keeping repo recipes machine-independent."""

    RECIPE = (b'executor_config:\n  volumes:\n'
              b'  - "{install_dir}/flash-next/ple:/ple"\n'
              b'command: |\n  sglang serve {model}\n')

    def test_expands_only_in_recipe_files(self):
        data = b'- "{install_dir}/flash-next/ple:/ple"\n'
        out = converge.expand_install_dir("sparkrun/recipes/m.yaml", data, "/home/u/AI")
        self.assertEqual(out, b'- "/home/u/AI/flash-next/ple:/ple"\n')
        # non-recipe files are untouched (litellm configs may carry braces)
        self.assertEqual(
            converge.expand_install_dir("litellm/model_config.yaml", data, "/h"), data)
        # no placeholder -> byte-identical (golden safety)
        plain = b"- /cache/huggingface:/cache/huggingface\n"
        self.assertEqual(
            converge.expand_install_dir("sparkrun/recipes/m.yaml", plain, "/h"), plain)
        # no concrete base -> left intact rather than corrupted
        self.assertEqual(
            converge.expand_install_dir("sparkrun/recipes/m.yaml", data, None), data)

    def test_write_files_expands_recipe_volumes(self):
        d = Path(tempfile.mkdtemp())
        fs = node_mod.LocalInstallFS(d)
        rendered = {
            "sparkrun/recipes/m.yaml": self.RECIPE,
            "litellm/model_config.yaml": b"model_list: []",
        }
        converge.write_files(types.SimpleNamespace(install_dir=str(d)),
                             rendered, dry_run=False, fs=fs)
        written = (d / "sparkrun" / "recipes" / "m.yaml").read_bytes()
        self.assertNotIn(b"{install_dir}", written)
        self.assertIn(str(d).encode() + b"/flash-next/ple:/ple", written)
        # sparkrun's own {model} placeholder must survive for launch time
        self.assertIn(b"sglang serve {model}", written)
        # non-recipe file byte-identical
        self.assertEqual((d / "litellm" / "model_config.yaml").read_bytes(),
                         b"model_list: []")


class TestTolerantStop(unittest.TestCase):
    """`sparkrun stop` failing with 'no running workload' is already-converged.

    A host whose state still records a model that is not (anymore) running
    (stopped out-of-band, never started, node reboot) must not abort the
    converge: the stop wrapper exits 0 for that specific outcome and only.
    """

    def _run_wrapper(self, fake_body: str) -> subprocess.CompletedProcess:
        d = Path(tempfile.mkdtemp())
        fake = d / "sparkrun"
        fake.write_text("#!/bin/sh\n" + fake_body + "\n")
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(d) + os.pathsep + os.environ.get("PATH", ""))
        argv = converge.tolerant_stop_argv("sparkrun", "/tmp/x/recipes/r.yaml",
                                           ["--hosts", "10.0.0.1"])
        return subprocess.run(argv, env=env, capture_output=True, text=True)

    def test_job_not_found_is_already_converged(self):
        p = self._run_wrapper(
            'echo "Error: No running workload matches intent 5b98ae96 on hosts ['
            '\'10.0.0.1\']" >&2\nexit 1')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("already stopped", p.stdout)

    def test_genuine_failure_still_fails(self):
        p = self._run_wrapper("echo 'Error: docker permission denied' >&2\nexit 1")
        self.assertEqual(p.returncode, 1)

    def test_successful_stop_passes_through(self):
        p = self._run_wrapper("echo 'stopped workload'\nexit 0")
        self.assertEqual(p.returncode, 0)
        self.assertIn("stopped workload", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
