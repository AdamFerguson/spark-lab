"""Phase 5 tests: the discovery framework (record, sources, registry) and
auto-conversion (deterministic + LLM-assisted, never applied)."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from sparklab.core import config as config_mod  # noqa: E402
from sparklab.core.discovery import (  # noqa: E402
    DiscoveredRecipe, DiscoveryRegistry, SourceError, build_source,
)
from sparklab.core.discovery import convert as cv  # noqa: E402
from sparklab.core.discovery.recipe import DiscoveredRecipe as _Rec  # noqa: E402,F401

REC = ROOT / ".sparkrun" / "registry.yaml"
COOK = ROOT / "cookbook" / "sglang.sample.json"

CFG = {
    "install_dir": "/tmp/x",
    "discovery": {
        "enabled": True,
        "sources": [
            {"kind": "sparkrun-registry", "alias": "local",
             "index": str(REC), "path": str(ROOT / "recipes")},
            {"kind": "sglang-cookbook", "alias": "cookbook",
             "path": str(COOK)},
        ],
    },
}


def _cfg(data, path="/tmp/cfg/config.yaml"):
    return config_mod.Config(data, Path(path), {})


def _registry(cfg=None):
    return DiscoveryRegistry(_cfg(cfg or CFG))


class TestRecord(unittest.TestCase):
    def test_ref_and_roundtrip(self):
        r = DiscoveredRecipe(source="local", reference="q", model_id="m",
                             image="i", tags=["a", "b"])
        self.assertEqual(r.ref, "local://q")
        d = r.to_dict()
        self.assertNotIn("body", d)  # body lazy
        back = DiscoveredRecipe.from_dict(d)
        self.assertEqual(back.ref, "local://q")
        self.assertEqual(back.tags, ["a", "b"])

    def test_from_dict_tolerates_unknown_keys(self):
        r = DiscoveredRecipe.from_dict({"source": "s", "reference": "r",
                                        "custom_thing": 42})
        self.assertEqual(getattr(r, "extra", {}).get("custom_thing"), 42)


class TestRegistry(unittest.TestCase):
    def test_loads_enabled_sources(self):
        self.assertEqual(_registry().source_aliases(), ["local", "cookbook"])

    def test_search_fans_out_and_dedups(self):
        records, errors = _registry().search("qwen")
        self.assertEqual(errors, [])
        refs = {r.ref for r in records}
        self.assertIn("local://qwen38-27b", refs)
        self.assertIn("cookbook://qwen3-8b", refs)

    def test_show_by_composite_ref_has_body(self):
        rec = _registry().show("local://qwen38-27b")
        self.assertEqual(rec.body.get("model"), "RadixArk/Qwen3.8-27B-NVFP4")

    def test_show_bare_ref_tries_all_sources(self):
        rec = _registry().show("qwen3-8b")  # found in the cookbook source
        self.assertEqual(rec.source, "cookbook")
        self.assertIn("model", rec.body)

    def test_per_source_error_isolation(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            tf.write("{ this is not json ")
            bad_path = tf.name
        bad = json.loads(json.dumps(CFG))
        bad["discovery"]["sources"] = [
            {"kind": "sglang-cookbook", "alias": "broken", "path": bad_path},
            {"kind": "sglang-cookbook", "alias": "good", "path": str(COOK)},
        ]
        records, errors = _registry(bad).list()
        self.assertEqual([e[0] for e in errors], ["broken"])
        self.assertTrue(records)  # the good source still returns results

    def test_unknown_source_kind_is_isolated(self):
        bad = json.loads(json.dumps(CFG))
        bad["discovery"]["sources"] = [{"kind": "does-not-exist", "alias": "x"}]
        self.assertEqual(_registry(bad).source_aliases(), [])

    def test_build_source_unknown_raises(self):
        with self.assertRaises(SourceError):
            build_source("does-not-exist", "x", {})


class TestConversion(unittest.TestCase):
    def test_deterministic_transform(self):
        entry = {"slug": "m", "model": "org/m", "image": "img:1",
                 "flags": {"mem_fraction_static": 0.9, "kv_cache_dtype": "fp8"}}
        cand = cv.deterministic_transform(entry)
        self.assertEqual(cand["model"], "org/m")
        self.assertEqual(cand["container"], "img:1")
        self.assertIn("--mem-fraction-static 0.9", cand["command"])
        self.assertIn("--kv-cache-dtype fp8", cand["command"])
        self.assertEqual(cand["defaults"]["mem_fraction_static"], 0.9)
        self.assertEqual(cv.validate_candidate(cand), [])

    def test_build_candidate_sparkrun_body_as_is(self):
        rec = DiscoveredRecipe(source="local", reference="x", body={
            "model": "org/m", "command": "sglang serve --model-path {model}"})
        cand = cv.build_candidate(rec)
        self.assertEqual(cand.get("_candidate_source"), "sparkrun-registry")
        self.assertEqual(cand["model"], "org/m")

    def test_build_candidate_cookbook_normalized(self):
        rec = DiscoveredRecipe(source="cookbook", reference="m", body={
            "model": "org/m", "image": "img:1", "flags": {"kv_cache_dtype": "fp8"}})
        cand = cv.build_candidate(rec)
        self.assertEqual(cand.get("_candidate_source"), "sglang-cookbook")
        self.assertIn("--kv-cache-dtype fp8", cand["command"])

    def test_build_candidate_llm_used_when_valid(self):
        good = yaml.safe_dump({"model": "org/m", "container": "img:1",
                               "command": "sglang serve --model-path {model}"})
        rec = DiscoveredRecipe(source="cookbook", reference="m",
                               body={"model": "org/m", "image": "img:1"})
        cand = cv.build_candidate(rec, llm_transport=lambda p: f"```yaml\n{good}\n```")
        self.assertEqual(cand.get("_candidate_source"), "llm")

    def test_build_candidate_llm_falls_back_to_deterministic(self):
        rec = DiscoveredRecipe(source="cookbook", reference="m",
                               body={"model": "org/m", "image": "img:1",
                                     "flags": {"kv_cache_dtype": "fp8"}})
        cand = cv.build_candidate(rec, llm_transport=lambda p: "not yaml at all")
        self.assertEqual(cand.get("_candidate_source"), "sglang-cookbook")
        self.assertIn("--kv-cache-dtype fp8", cand["command"])

    def test_validate_candidate_flags_missing(self):
        self.assertEqual(len(cv.validate_candidate({})), 3)


class TestRecipesCommands(unittest.TestCase):
    """Command-level tests through the real CLI dispatch (read-only, no runtime I/O)."""

    def _run(self, *argv):
        from sparklab import cli
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.yaml"
            data = json.loads(json.dumps(CFG))
            cfg.write_text(yaml.safe_dump(data, sort_keys=False))
            return cli.main(list(argv) + ["--config", str(cfg)])

    def test_recipes_list(self):
        self.assertEqual(self._run("recipes", "list"), 0)

    def test_recipes_search(self):
        self.assertEqual(self._run("recipes", "search", "qwen"), 0)

    def test_recipes_show_json(self):
        self.assertEqual(self._run("recipes", "show", "local://qwen38-27b", "--json"), 0)

    def test_recipes_convert_dry_run(self):
        self.assertEqual(self._run("recipes", "convert", "cookbook://qwen3-8b",
                                   "--dry-run"), 0)

    def test_recipes_convert_writes_valid_candidate(self):
        from sparklab import cli
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.yaml"
            cfg.write_text(yaml.safe_dump(json.loads(json.dumps(CFG)), sort_keys=False))
            out = Path(d) / "cand.yaml"
            rc = cli.main(["recipes", "convert", "cookbook://qwen3-8b",
                           "--out", str(out), "--config", str(cfg)])
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            data = yaml.safe_load(out.read_text())
            self.assertEqual(data["model"], "Qwen/Qwen3-8B")
            self.assertIn("command", data)
            self.assertIn("candidate", out.read_text().lower())  # clearly marked


if __name__ == "__main__":
    unittest.main(verbosity=2)
