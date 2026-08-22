"""Built-in source: the **SGLang cookbook** (ADR 0003).

Reads a curated collection of SGLang model entries (model id, image, serve
flags, hardware notes). An entry is *not* a sparkrun document -- the record's
``body`` is the cookbook entry itself, and normalizing it into a sparkrun
*candidate* is a separate, later step (``recipes convert``). Default is the
in-repo sample cookbook (``cookbook/sglang.sample.json``); redirect with the
``path`` option.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..discovery.recipe import DiscoveredRecipe
from ..discovery.source import RecipeSource, SourceError


class SglangCookbookSource(RecipeSource):
    kind = "sglang-cookbook"

    def _repo_root(self) -> Path:
        root = getattr(self.config, "repo_root", None)
        return Path(root).expanduser() if root else Path.cwd()

    def _cookbook_path(self) -> Path:
        p = self.options.get("path")
        return Path(p).expanduser() if p else self._repo_root() / "cookbook" / "sglang.sample.json"

    def _entries(self) -> List[Dict[str, Any]]:
        path = self._cookbook_path()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise SourceError(f"{self.alias}: bad cookbook {path}: {e}")
        return data.get("entries", data) if isinstance(data, dict) else data

    def _record(self, e: Dict[str, Any], with_body: bool = False) -> DiscoveredRecipe:
        rec = DiscoveredRecipe(
            source=self.alias,
            reference=str(e.get("slug") or e.get("model") or "entry"),
            name=str(e.get("name") or e.get("model") or "entry"),
            description=str(e.get("description", "")),
            origin=str(self._cookbook_path()),
            model_id=str(e.get("model", "")),
            image=str(e.get("image", "")),
            hardware=str(e.get("hardware", "")),
            tags=list(e.get("tags", [])),
            freshness=self.options.get("cache_ttl", ""),
        )
        if with_body:
            rec.body = e
        return rec

    def list(self, **filters) -> List[DiscoveredRecipe]:
        return [self._record(e) for e in self._entries()]

    def search(self, query: str, **filters) -> List[DiscoveredRecipe]:
        q = (query or "").strip().lower()
        out = []
        for e in self._entries():
            hay = " ".join([str(e.get("slug", "")), str(e.get("model", "")),
                            str(e.get("description", "")), " ".join(e.get("tags", []))]).lower()
            if not q or all(tok in hay for tok in q.split()):
                out.append(self._record(e))
        return out

    def show(self, reference: str) -> DiscoveredRecipe:
        for e in self._entries():
            if str(e.get("slug") or e.get("model") or "entry") == reference:
                return self._record(e, with_body=True)
        raise SourceError(f"{self.alias}: no entry '{reference}'")
