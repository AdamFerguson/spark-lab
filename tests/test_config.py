"""Unit tests for config loading, validation, defaults, and secret resolution."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import load, load_dotenv  # noqa: E402


def _cfg(text):
    d = Path(tempfile.mkdtemp())
    p = d / "config.yaml"
    p.write_text(text)
    return p


class TestConfig(unittest.TestCase):
    def test_accessors_and_defaults(self):
        p = _cfg("install:\n  install_dir: ~/AI\n  hosts: [127.0.0.1]\n"
                 "model:\n  recipe_name: qwen\n")
        cfg = load(str(p))
        self.assertTrue(str(cfg.install_dir).endswith("/AI"))
        self.assertEqual(cfg.hosts, ["127.0.0.1"])
        self.assertFalse(cfg.is_cluster)
        self.assertEqual(cfg.recipe_name, "qwen")
        self.assertEqual(cfg.cluster_name, "mylab")  # default
        self.assertEqual(cfg.model_api_base, "http://host.docker.internal:30000/v1")

    def test_cluster_detection(self):
        cfg = load(str(_cfg("install:\n  hosts: [a, b]\n")))
        self.assertTrue(cfg.is_cluster)
        self.assertEqual(len(cfg.hosts), 2)

    def test_secret_resolved_from_env_file(self):
        d = Path(tempfile.mkdtemp())
        (d / "c.yaml").write_text("model:\n  hf_token_env: HF_TOKEN\n")
        (d / ".env").write_text("HF_TOKEN=mytoken\n")
        self.assertEqual(load(str(d / "c.yaml")).secret("HF_TOKEN"), "mytoken")

    def test_secret_missing_is_empty(self):
        cfg = load(str(_cfg("model:\n  hf_token_env: NOPE\n")))
        self.assertEqual(cfg.secret("NOPE"), "")
        self.assertEqual(cfg.secret(None), "")

    def test_load_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            load(str(Path(tempfile.mkdtemp()) / "nope.yaml"))

    def test_load_dotenv_parsing(self):
        p = Path(tempfile.mkdtemp()) / "e"
        p.write_text("# comment\n\nA=1\nB = 'two'\nC=\"three\"\nNOEQUALS\n")
        self.assertEqual(load_dotenv(p), {"A": "1", "B": "two", "C": "three"})

    def test_load_dotenv_missing_file_is_empty(self):
        self.assertEqual(load_dotenv(Path(tempfile.mkdtemp()) / "nope"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
