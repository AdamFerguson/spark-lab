"""The discovery registry (ADR 0003).

The only object that knows *how* to build sources. It reads the ``discovery:``
section of ``config.yaml``, maps each declared kind to a built-in adapter (or a
third-party entry point under ``sparklab.recipe_sources``), and fans
``search``/``list``/``show`` out to the enabled sources with **per-source error
isolation** -- a dead source never takes down the rest. A light on-disk cache
(optional ``cache_dir``) stores fetched bodies keyed by source + freshness.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .recipe import DiscoveredRecipe
from .source import RecipeSource, SourceError
from .sparkrun_registry import SparkrunRegistrySource
from .sglang_cookbook import SglangCookbookSource

_BUILTIN: Dict[str, type] = {
    "sparkrun-registry": SparkrunRegistrySource,
    "sglang-cookbook": SglangCookbookSource,
}


def _entry_points(group: str):
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return []
    try:
        eps = entry_points(group=group)
        return list(eps)  # 3.10+ supports group= and returns a list
    except TypeError:  # pragma: no cover - older API shape
        return list(entry_points().get(group, []))


def build_source(kind: str, alias: str, options: Dict[str, Any] = None,
                 config=None, runtime=None) -> RecipeSource:
    """Construct a source by kind (built-in first, then entry points)."""
    options = options or {}
    cls = _BUILTIN.get(kind)
    if cls is None:
        for ep in _entry_points("sparklab.recipe_sources"):
            if ep.name == kind:
                cls = ep.load()
                break
    if cls is None:
        raise SourceError(f"unknown recipe-source kind '{kind}'")
    return cls(alias=alias, options=options, config=config, runtime=runtime)


class DiscoveryRegistry:
    def __init__(self, config=None, runtime=None, cache_dir: Optional[Path] = None):
        self.config = config
        self.runtime = runtime
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._sources: Dict[str, RecipeSource] = {}
        self._load()

    def _load(self):
        disc = {}
        if self.config is not None:
            disc = self.config.discovery() or {}
        if not disc.get("enabled", True):
            return
        for entry in disc.get("sources", []):
            kind = entry.get("kind")
            alias = str(entry.get("alias") or kind)
            try:
                self._sources[alias] = build_source(kind, alias, entry, self.config, self.runtime)
            except Exception:
                # isolated: an unknown/broken kind does not break the others
                continue

    @property
    def sources(self) -> List[RecipeSource]:
        return list(self._sources.values())

    def source_aliases(self) -> List[str]:
        return list(self._sources)

    def get(self, alias: str) -> Optional[RecipeSource]:
        return self._sources.get(alias)

    # -- fan-out with per-source error isolation ----------------------------
    def _iter(self, source: Optional[str]) -> List[RecipeSource]:
        if source:
            s = self._sources.get(source)
            if s is None:
                raise SourceError(f"unknown source alias '{source}' "
                                  f"(available: {', '.join(self.source_aliases()) or 'none'})")
            return [s]
        return self.sources

    def search(self, query: str, source: Optional[str] = None
               ) -> Tuple[List[DiscoveredRecipe], List[Tuple[str, str]]]:
        results, errors = [], []
        for s in self._iter(source):
            try:
                results.extend(s.search(query))
            except Exception as e:  # isolated
                errors.append((s.alias, str(e)))
        return self._dedup(results), errors

    def list(self, source: Optional[str] = None
             ) -> Tuple[List[DiscoveredRecipe], List[Tuple[str, str]]]:
        results, errors = [], []
        for s in self._iter(source):
            try:
                results.extend(s.list())
            except Exception as e:
                errors.append((s.alias, str(e)))
        return self._dedup(results), errors

    def show(self, ref: str) -> DiscoveredRecipe:
        alias, native = (ref.split("://", 1) + [None])[:2] if "://" in ref else (None, ref)
        s = self._sources.get(alias) if alias else None
        if s is not None:
            return self._cached_show(s, native)
        # no alias (or unknown): try each source in turn
        last = None
        for src in self._sources.values():
            try:
                return self._cached_show(src, native)
            except Exception as e:
                last = e
        if last:
            raise last
        raise SourceError(f"could not resolve '{ref}'")

    @staticmethod
    def _dedup(records: List[DiscoveredRecipe]) -> List[DiscoveredRecipe]:
        seen, out = set(), []
        for r in records:
            key = (r.model_id, r.image, r.reference)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    # -- light body cache ---------------------------------------------------
    def _cache_key(self, source: RecipeSource, ref: str) -> Path:
        safe = lambda x: "".join(c if c.isalnum() or c in "-_." else "_" for c in str(x))
        return self.cache_dir / f"{safe(source.alias)}__{safe(ref)}__{safe(source.freshness())}.json"

    def _cached_show(self, source: RecipeSource, ref: str) -> DiscoveredRecipe:
        if self.cache_dir is None:
            return source.show(ref)
        key = self._cache_key(source, ref)
        if key.is_file():
            try:
                return DiscoveredRecipe.from_dict(json.loads(key.read_text()))
            except (json.JSONDecodeError, ValueError):
                pass
        rec = source.show(ref)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            key.write_text(json.dumps(rec.to_dict(with_body=True), sort_keys=True))
        except OSError:
            pass
        return rec
