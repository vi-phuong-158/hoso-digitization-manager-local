from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..catalog import Catalog, find_catalog_path, load_catalog
from ..models import UNKNOWN
from .db import Database


@dataclass(frozen=True)
class TaxonomyItem:
    code: str
    name: str
    priority: int
    active: bool = True
    default_applicability: str = "CHUA_XAC_DINH"


class TaxonomyAdapter:
    """Read-only bridge to the existing official catalog."""

    def __init__(self, catalog: Catalog, path: Path):
        self.catalog = catalog
        self.path = path
        self.items = tuple(
            TaxonomyItem(
                code=item.id,
                name=item.name_vi,
                priority=item.priority if item.priority in {1, 2, 3} else 3,
            )
            for item in catalog.all_types()
        )
        self._by_code = {item.code: item for item in self.items}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "TaxonomyAdapter":
        resolved = Path(path) if path else find_catalog_path()
        try:
            catalog = load_catalog(str(resolved))
        except Exception:
            # Safe fallback for a packaged executable where app.catalog may not
            # be importable from its original source location.
            raw = json.loads(resolved.read_text(encoding="utf-8"))
            catalog = Catalog(raw, resolved)
        return cls(catalog, resolved)

    def get(self, code: str) -> TaxonomyItem | None:
        return self._by_code.get(str(code))

    def require(self, code: str) -> TaxonomyItem:
        item = self.get(code)
        if item is None:
            raise ValueError(f"taxonomy code không có trong catalog: {code}")
        return item

    def is_valid(self, code: str | None) -> bool:
        return code == UNKNOWN or (code is not None and str(code) in self._by_code)

    def seed(self, db: Database) -> int:
        with db.session() as conn:
            for item in self.items:
                conn.execute(
                    """INSERT INTO taxonomy_items(code,name,priority,active,default_applicability)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                       priority=excluded.priority, active=excluded.active""",
                    (item.code, item.name, item.priority, int(item.active), item.default_applicability),
                )
        return len(self.items)

    def as_dicts(self) -> list[dict]:
        return [
            {
                "code": item.code,
                "name": item.name,
                "priority": item.priority,
                "active": item.active,
                "default_applicability": item.default_applicability,
            }
            for item in self.items
        ]
