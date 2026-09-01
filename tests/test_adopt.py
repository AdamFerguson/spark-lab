"""`spark-lab adopt` -- take over an existing running install without disturbing it."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparklab import cli  # noqa: E402
from sparklab.core import config, render, state, converge  # noqa: E402
from tests.helpers import REFERENCE_ENV, config_text  # noqa: E402


class AdoptTest(unittest.TestCase):
    def _materialise(self, base: Path, install_dir: str) -> str:
        """Write a config pointing at install_dir + materialise the 'live' install."""
        text = config_text(install_dir)
        repo = base / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        cfg_path = repo / "config.yaml"
        cfg_path.write_text(text)
        (repo / ".env").write_text(REFERENCE_ENV)
        out = Path(install_dir)
        out.mkdir(parents=True, exist_ok=True)
        for rel, data in render.render(config.load(str(cfg_path)), out).items():
            (out / rel).write_bytes(data)
        return str(cfg_path)

    def _adopt(self, cfg_path, state_dir, dry=False):
        argv = ["adopt"] + (["--dry-run"] if dry else []) + ["--config", cfg_path]
        with mock.patch.object(config.Config, "state_dir",
                               new_callable=mock.PropertyMock, return_value=state_dir):
            return cli.main(argv)

    def _state(self, state_dir) -> dict:
        return state.State(state_dir).load()

    def test_adopt_converged_matching_install(self):
        base = Path(tempfile.mkdtemp())
        install = str(base / "live")
        cfg_path = self._materialise(base, install)
        self.assertEqual(self._adopt(cfg_path, base / "state"), 0)
        st = self._state(base / "state")
        self.assertIn("sparkrun/recipes/qwen.yaml", st["files"])
        self.assertEqual(st["model"]["name"], "qwen")
        self.assertEqual(st["model"]["hash"], st["files"]["sparkrun/recipes/qwen.yaml"])

    def test_adopt_flags_drift_and_records_on_disk(self):
        base = Path(tempfile.mkdtemp())
        install = base / "live"
        cfg_path = self._materialise(base, str(install))
        self._adopt(cfg_path, base / "state")
        target = install / "litellm" / "docker-compose.yml"
        target.write_text(target.read_text() + "# live drift\n")
        self.assertEqual(self._adopt(cfg_path, base / "state"), 0)
        st = self._state(base / "state")
        # adopted the ON-DISK (drifted) hash, not the rendered one
        self.assertEqual(st["files"]["litellm/docker-compose.yml"],
                         state.sha256_bytes(target.read_bytes()))

    def test_adopt_missing_file_not_recorded(self):
        base = Path(tempfile.mkdtemp())
        install = base / "live"
        cfg_path = self._materialise(base, str(install))
        self._adopt(cfg_path, base / "state")
        (install / "litellm" / "docker-compose.yml").unlink()
        self.assertEqual(self._adopt(cfg_path, base / "state"), 0)
        self.assertNotIn("litellm/docker-compose.yml", self._state(base / "state")["files"])

    def test_adopt_wrong_recipe_records_no_model(self):
        base = Path(tempfile.mkdtemp())
        install = base / "live"
        cfg_path = self._materialise(base, str(install))
        # point the config's active model at an alias with no recipe on disk
        cfg = Path(cfg_path)
        cfg.write_text(cfg.read_text().replace("  qwen:", "  nonexistent:"))
        self.assertEqual(self._adopt(cfg_path, base / "state"), 0)
        self.assertNotIn("model", self._state(base / "state"))

    def test_adopt_dry_run_writes_no_state(self):
        base = Path(tempfile.mkdtemp())
        install = base / "live"
        cfg_path = self._materialise(base, str(install))
        st = base / "state"
        self.assertEqual(self._adopt(cfg_path, st, dry=True), 0)
        self.assertFalse((st / "state.json").exists())

    def test_routine_apply_after_adopt_does_not_restart_model(self):
        """Adoption + a routine apply (no --apply) leaves the model untouched."""
        base = Path(tempfile.mkdtemp())
        install = base / "live"
        cfg_path = self._materialise(base, str(install))
        st = base / "state"
        self._adopt(cfg_path, st)
        cfg = config.load(cfg_path)
        rendered = render.render(cfg, Path(tempfile.mkdtemp()))
        st_obj = state.State(st)
        plan = converge.build_plan(cfg, rendered, st_obj.files, st_obj.model, allow_restart=False)
        self.assertTrue(plan.model_converged)
        self.assertFalse(plan.model_restart_pending)
        self.assertFalse(any("Stop model" in d for d, _ in plan.commands))


if __name__ == "__main__":
    unittest.main()
