from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TaxonomyRecord:
    code: str
    name: str
    priority: int
    filename_base: str

    @property
    def catalog_filename_slug(self) -> str:
        return self.filename_base.split(".", 1)[1]


class Taxonomy:
    """Read-only view of the versioned 104-type filesystem contract."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        rows = raw.get("document_types")
        if not isinstance(rows, list):
            raise ValueError("document_types.json không có document_types[].")
        records = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("id", ""))
            name = str(row.get("name_vi", ""))
            filename_base = str(row.get("filename_base", ""))
            if not code or not name or not filename_base:
                raise ValueError("Taxonomy có item thiếu id, name_vi hoặc filename_base.")
            records.append(TaxonomyRecord(code, name, int(row.get("priority", 3)), filename_base))
        self.items = tuple(records)
        self._by_code = {item.code: item for item in self.items}
        if len(self._by_code) != 104:
            raise ValueError("Taxonomy Manager phải có đúng 104 loại.")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Taxonomy":
        return cls(path or (REPOSITORY_ROOT / "document_types.json"))

    def get(self, code: str | None) -> TaxonomyRecord | None:
        return self._by_code.get(str(code)) if code is not None else None

    def require(self, code: str) -> TaxonomyRecord:
        item = self.get(code)
        if item is None:
            raise ValueError(f"taxonomy code không có trong catalog: {code}")
        return item

    def is_valid(self, code: str | None) -> bool:
        return self.get(code) is not None