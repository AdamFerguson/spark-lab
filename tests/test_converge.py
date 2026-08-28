"""Unit tests for the converge logic (no node, no subprocess, no real config).

These pin down the "stable to update" contract:
  * no-op stays a no-op (no model restart)
  * add / remove / switch a recipe converges (stop the old, start the new)
  * a recipe change that is not restarted stays *pending* (does not drift)
  * the recorded state tracks which recipe the model is actually running

Run with:  python tests/test_converge.py
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# make `lib` importable when run as a plain script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparklab.core import converge, state  # noqa: E402
from tests.helpers import FakeRuntime  # noqa: E402


def make_cfg(recipe="mymodel", cluster=False):
    c = types.SimpleNamespace()
    c.recipe_name = recipe
    c.install_dir = "/tmp/sparklab-test"
    c.is_cluster = cluster
    c.cluster_name = "spark"
    c.hosts = ["10.0.0.1", "10.0.0.2"] if cluster else []
    c.model = {"port": 30000}      # active model definition (probe reads .port)
    c.tailscale = lambda: {"enabled": False}
    c.cloudflare = lambda: {"enabled": False}
    c.prometheus = lambda: {"port": 9090}
    c.monitoring_role = lambda: "full"
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

    def tearDown(self):
        self._p.stop()

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

    # 2b. Fresh model: starts without --apply, records state ----------------
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
        self.assertTrue(any("--apply" in n for n in plan.notes))
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
        self.assertIn(("sparkrun/recipes/qwen.yaml", "removed"), plan.file_changes)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
