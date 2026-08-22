"""Built-in source: a **sparkrun registry** (ADR 0003).

Reads a registry of ready-to-run sparkrun recipes. The default is the in-repo
registry shipped with this project (a ``.sparkrun/registry.yaml`` index plus
``recipes/*.yaml`` recipe files). Point it elsewhere with the ``path``/``index``
options -- enabling/redirecting a source is a config change, not a code change.
Read-only: ``list``/``search`` use the index; ``show`` reads the recipe file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

from ..discovery.recipe import DiscoveredRecipe
from ..discovery.source import RecipeSource, SourceError


class SparkrunRegistrySource(RecipeSource):
    kind = "sparkrun-registry"

    def __init__(self, alias: str, options: Dict[str, Any] = None, config=None, runtime=None):
        super().__init__(alias, options, config, runtime)

    # -- path resolution ----------------------------------------------------
    def _repo_root(self) -> Path:
        root = getattr(self.config, "repo_root", None)
        return Path(root).expanduser() if root else Path.cwd()

    def _index_path(self) -> Path:
        p = self.options.get("index")
        return Path(p).expanduser() if p else self._repo_root() / ".sparkrun" / "registry.yaml"

    def _recipes_dir(self) -> Path:
        p = self.options.get("path")
        return Path(p).expanduser() if p else self._repo_root() / "recipes"

    # -- data ----------------------------------------------------------------
    def _index(self) -> Dict[str, Any]:
        idx = self._index_path()
        if not idx.is_file():
            return {}
        try:
            return yaml.safe_load(idx.read_text()) or {}
        except yaml.YAMLError as e:
            raise SourceError(f"{self.alias}: bad registry index {idx}: {e}")

    def _entries(self) -> Dict[str, Dict[str, Any]]:
        """Return {reference: entry} from the index, else scan the recipes dir."""
        idx = self._index()
        entries = idx.get("recipes")
        if isinstance(entries, dict) and entries:
            return entries
        # fallback: derive a minimal entry from each recipe file
        out: Dict[str, Dict[str, Any]] = {}
        d = self._recipes_dir()
        if d.is_dir():
            for f in sorted(d.glob("*.yaml")):
                data = yaml.safe_load(f.read_text()) or {}
                out[f.stem] = {"file": f.name,
                               "model": data.get("hf_model", ""),
                               "image": data.get("container", ""),
                               "tags": data.get("tags", []),
                               "hardware": data.get("hardware", ""),
                               "description": data.get("description", "")}
        return out

    def _record(self, ref: str, entry: Dict[str, Any]) -> DiscoveredRecipe:
        return DiscoveredRecipe(
            source=self.alias, reference=ref, name=entry.get("name") or ref,
            description=entry.get("description", ""),
            origin=str(self._recipes_dir()),
            model_id=str(entry.get("model", "")),
            image=str(entry.get("image", "")),
            hardware=str(entry.get("hardware", "")),
            tags=list(entry.get("tags", [])),
            freshness=self.options.get("cache_ttl", ""),
        )

    # -- contract ------------------------------------------------------------
    def list(self, **filters) -> List[DiscoveredRecipe]:
        return [self._record(r, e) for r, e in sorted(self._entries().items())]

    def search(self, query: str, **filters) -> List[DiscoveredRecipe]:
        q = (query or "").strip().lower()
        out = []
        for ref, entry in sorted(self._entries().items()):
            hay = " ".join([ref, str(entry.get("name", "")), str(entry.get("model", "")),
                            str(entry.get("description", "")), " ".join(entry.get("tags", []))]
                           ).lower()
            if not q or all(tok in hay for tok in q.split()):
                out.append(self._record(ref, entry))
        return out

    def show(self, reference: str) -> DiscoveredRecipe:
        entries = self._entries()
        if reference not in entries:
            raise SourceError(f"{self.alias}: no recipe '{reference}'")
        entry = entries[reference]
        rec = self._record(reference, entry)
        file = entry.get("file")
        if file:
            # `file` is relative to the registry root (the dir containing recipes/)
            bases = (self._index_path().parent.parent, self._repo_root(), self._recipes_dir())
            for base in bases:
                cand = base / file
                if cand.is_file():
                    rec.body = yaml.safe_load(cand.read_text()) or {}
                    break
        return rec
