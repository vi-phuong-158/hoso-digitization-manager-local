from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.manager_core.taxonomy import Taxonomy, TaxonomyRecord
from .db import Database


@dataclass(frozen=True)
class TaxonomyItem:
    code: str
    name: str
    priority: int
    filename_base: str
    active: bool = True
    default_applicability: str = "CHUA_XAC_DINH"

    @property
    def catalog_filename_slug(self) -> str:
        return self.filename_base.split(".", 1)[1]


class TaxonomyAdapter:
    """Manager-local adapter for the shared versioned taxonomy contract."""

    def __init__(self, taxonomy: Taxonomy):
        self.catalog = taxonomy
        self.path = taxonomy.path
        self.items = tuple(
            TaxonomyItem(record.code, record.name, record.priority if record.priority in {1, 2, 3} else 3, record.filename_base)
            for record in taxonomy.items
        )
        self._by_code = {item.code: item for item in self.items}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "TaxonomyAdapter":
        return cls(Taxonomy.load(path))

    def get(self, code: str | None) -> TaxonomyItem | None:
        return self._by_code.get(str(code)) if code is not None else None

    def require(self, code: str) -> TaxonomyItem:
        item = self.get(code)
        if item is None:
            raise ValueError(f"taxonomy code không có trong catalog: {code}")
        return item

    def is_valid(self, code: str | None) -> bool:
        return code == "UNKNOWN" or (code is not None and str(code) in self._by_code)

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
        return [item.__dict__ for item in self.items]