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

# make `lib` importable when run as a plain script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparklab.core import converge, state  # noqa: E402

# Never touch the real sparkrun during tests.
converge.find_sparkrun = lambda: "sparkrun"


def make_cfg(recipe="mymodel", cluster=False):
    c = types.SimpleNamespace()
    c.recipe_name = recipe
    c.install_dir = "/tmp/sparklab-test"
    c.is_cluster = cluster
    c.cluster_name = "spark"
    c.hosts = ["10.0.0.1", "10.0.0.2"] if cluster else []
    c.tailscale = lambda: {"enabled": False}
    c.cloudflare = lambda: {"enabled": False}
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
        self.assertTrue(any("run mymodel --ensure" in " ".join(map(str, a)) for d, a in plan.commands))
        # state now records the running model, so a later content change is caught
        new_model = converge.compute_model_after_apply(
            None, "mymodel", plan.current_hash, converged_after(plan, False))
        self.assertEqual(new_model, {"name": "mymodel", "hash": plan.current_hash})

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
        self.assertTrue(any("run llama --ensure" in " ".join(map(str, a)) for d, a in plan.commands))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
