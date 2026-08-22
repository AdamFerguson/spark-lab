"""Light CLI-level smoke tests: argument parsing + safe no-op command paths."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab import cli  # noqa: E402
from tests.helpers import REFERENCE_CONFIG, REFERENCE_ENV  # noqa: E402


class TestCliSmoke(unittest.TestCase):
    def _config(self):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(REFERENCE_CONFIG)
        (d / ".env").write_text(REFERENCE_ENV)
        return str(d / "config.yaml")

    def test_apply_dry_run_succeeds_without_a_node(self):
        self.assertEqual(cli.main(["apply", "--dry-run", "--config", self._config()]), 0)

    def test_teardown_refuses_without_yes(self):
        self.assertEqual(cli.main(["teardown", "--config", self._config()]), 1)

    def test_missing_command_exits(self):
        with self.assertRaises(SystemExit):
            cli.main([])

    def test_unknown_command_exits(self):
        with self.assertRaises(SystemExit):
            cli.main(["bogus"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
