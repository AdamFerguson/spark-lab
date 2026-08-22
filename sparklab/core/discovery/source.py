"""The ``RecipeSource`` contract (ADR 0003).

A discovery source is read-only and non-disruptive: it enumerates recipes
(``list``), answers queries (``search``), and fetches a full record (``show``).
Any I/O it needs (shelling out to a registry CLI, pulling a cookbook) goes
through the runtime seam (ADR 0002) so it is testable with the fake runtime and
never mutates node state. Sources signal their own failures as ``SourceError``;
the registry isolates them so one dead source never takes down the others.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional

from .recipe import DiscoveredRecipe


class SourceError(Exception):
    """A per-source discovery failure (unreachable, malformed, auth missing)."""


class RecipeSource(abc.ABC):
    #: the built-in kind this source implements (used by the registry loader).
    kind: str = "abstract"

    def __init__(self, alias: str, options: Optional[Dict[str, Any]] = None,
                 config=None, runtime=None):
        self.alias = alias
        self.options = options or {}
        self.config = config
        self.runtime = runtime

    @abc.abstractmethod
    def list(self, **filters) -> List[DiscoveredRecipe]:
        """Enumerate recipes (uniform records, no bodies)."""

    @abc.abstractmethod
    def search(self, query: str, **filters) -> List[DiscoveredRecipe]:
        """Free-text / structured query; returns matching records (no bodies)."""

    def show(self, reference: str) -> DiscoveredRecipe:
        """Fetch the record *with* its full body. Default: list + filter."""
        for rec in self.list():
            if rec.reference == reference:
                return rec
        raise SourceError(f"{self.alias}: no recipe '{reference}'")

    def freshness(self) -> str:
        """A freshness/collection identity used for cache revalidation."""
        return self.options.get("cache_ttl", "")
