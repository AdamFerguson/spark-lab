"""The uniform discovered-recipe record (ADR 0003).

Every discovery source emits records of this shape regardless of origin, so the
``recipes`` commands and the auto-conversion step work against one contract.
It is intentionally source-agnostic and **additive**: ``from_dict`` tolerates
unknown keys (kept in ``extra``) so a third-party adapter can add fields without
breaking readers. The ``body`` (the full native recipe document) is lazy --
``list``/``search`` omit it; ``show`` fetches it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_KNOWN = ("source", "reference", "name", "description", "origin", "model_id",
          "serving_framework", "image", "hardware", "tags", "freshness", "body")


@dataclass
class DiscoveredRecipe:
    source: str = ""
    reference: str = ""
    name: str = ""
    description: str = ""
    origin: str = ""
    model_id: str = ""
    serving_framework: str = "sglang"
    image: str = ""
    hardware: str = ""
    tags: List[str] = field(default_factory=list)
    freshness: str = ""
    body: Optional[Dict[str, Any]] = None

    @property
    def ref(self) -> str:
        """Stable composite reference: ``<source>://<reference>``."""
        return f"{self.source}://{self.reference}"

    def to_dict(self, with_body: bool = False) -> Dict[str, Any]:
        out = {k: getattr(self, k) for k in _KNOWN if k != "body"}
        out["tags"] = list(self.tags)
        if with_body and self.body is not None:
            out["body"] = self.body
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DiscoveredRecipe":
        """Build a record from a dict, tolerating unknown keys (kept in ``extra``)."""
        known = {k: d.get(k) for k in _KNOWN if k in d}
        tags = known.get("tags") or []
        known["tags"] = list(tags) if isinstance(tags, (list, tuple)) else [str(tags)]
        body = d.get("body")
        rec = cls(**{k: v for k, v in known.items() if k != "body"}, body=body)
        # retain unknown keys so nothing from a third-party source is lost
        extra = {k: v for k, v in d.items() if k not in _KNOWN}
        if extra:
            rec.extra = extra
        return rec

    def to_json(self, with_body: bool = False) -> str:
        import json
        return json.dumps(self.to_dict(with_body=with_body), indent=2, sort_keys=True)
