"""Recipe-discovery framework (ADR 0003).

A source-agnostic record type (``DiscoveredRecipe``), the ``RecipeSource``
contract, and the ``DiscoveryRegistry`` that loads enabled sources from
``config.yaml`` and fans queries out to them with per-source error isolation.
Built-in adapters: ``sparkrun-registry`` and ``sglang-cookbook``.
"""
from __future__ import annotations

from .recipe import DiscoveredRecipe
from .source import RecipeSource, SourceError
from .registry import DiscoveryRegistry, build_source

__all__ = [
    "DiscoveredRecipe", "RecipeSource", "SourceError",
    "DiscoveryRegistry", "build_source",
]
