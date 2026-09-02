"""Golden regression: pin rendered output + the apply command sequence.

These are the "parity" anchors for this repo: they assert the engine's output is
stable so Phase 3's refactor (``lib`` -> ``sparklab`` move, new console entry
point) is provably behavior-preserving. (Parity for the shell *helpers* --
``capture`` / ``secret-scan`` -- lands in Phase 3 alongside the ported commands.)

If a golden starts failing after an intentional change, review it, confirm the
new output is correct, then regenerate:  ``python3 tests/gen_golden.py``.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import config as config_mod, converge, render  # noqa: E402
from tests.helpers import (  # noqa: E402
    GOLDEN_DIR,
    REFERENCE_CONFIG,
    REFERENCE_ENV,
    SECRET_DUMMY,
    V3_CLUSTER_CONFIG,
)


class TestParity(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _render_and_plan(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(REFERENCE_CONFIG)
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        rendered = render.render(cfg, d / "deploy")
        plan = converge.build_plan(cfg, rendered, {}, None, allow_restart=True)
        return cfg, rendered, plan

    def test_rendered_file_hashes_match_golden(self):
        _cfg, rendered, _plan = self._render_and_plan()
        expected = json.loads((GOLDEN_DIR / "expected_sha256.json").read_text())
        actual = {rel: hashlib.sha256(data).hexdigest() for rel, data in rendered.items()}
        self.assertEqual(actual, expected)

    def test_rendered_recipe_matches_golden(self):
        cfg, rendered, _plan = self._render_and_plan()
        expected = (GOLDEN_DIR / "expected_recipe.yaml").read_text()
        self.assertEqual(
            rendered[f"sparkrun/recipes/{cfg.recipe_name}.yaml"].decode("utf-8"),
            expected,
        )

    def test_fresh_apply_command_sequence_matches_golden(self):
        _cfg, _rendered, plan = self._render_and_plan()
        expected = json.loads((GOLDEN_DIR / "expected_commands.json").read_text())
        actual = [[desc, [str(x) for x in argv]] for desc, argv in plan.commands]
        self.assertEqual(actual, expected)

    def test_golden_reference_config_is_the_reference(self):
        self.assertEqual(
            (GOLDEN_DIR / "reference_config.yaml").read_text(),
            REFERENCE_CONFIG,
        )

    def test_v3_host_views_render_match_golden(self):
        """Per-host views of the fixed v3 cluster config render stably (ADR 0008):\n        the host overrides (instance label, per-host model params) are visible in\n        the rendered bytes, and both hosts' file sets are pinned."""
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(V3_CLUSTER_CONFIG)
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        expected = json.loads((GOLDEN_DIR / "expected_v3_sha256.json").read_text())
        actual: dict = {}
        for host in (s.name for s in cfg.host_specs):
            view = cfg.view_for(host)
            rendered = render.render(view, d / f"v3-{host}")
            actual[host] = {rel: hashlib.sha256(data).hexdigest() for rel, data in rendered.items()}
        self.assertEqual(actual, expected)
        # the two hosts really differ where the config says they should
        a = actual["alpha"]["sparkrun/recipes/qwen.yaml"]
        b = actual["beta"]["sparkrun/recipes/qwen.yaml"]
        self.assertNotEqual(a, b)
        self.assertNotEqual(
            actual["alpha"]["litellm/prometheus.yml"],
            actual["beta"]["litellm/prometheus.yml"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
