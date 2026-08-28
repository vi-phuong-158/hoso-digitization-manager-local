"""Phase chung — đọc taxonomy từ document_types.json.

document_types.json là NGUỒN CHÂN LÝ DUY NHẤT cho 104 loại tài liệu.
Module này chỉ đọc; không bao giờ ghi/sửa file đó.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .models import UNKNOWN, PipelineError

CATALOG_FILENAME = "document_types.json"

# filename_base hợp lệ: "<STT>.<Ten_khong_dau_gach_duoi>"
FILENAME_BASE_RE = re.compile(r"^\d{2,3}\.[A-Za-z0-9_]+$")

# Các nhóm dễ nhầm — trích nguyên văn từ AGENTS.md mục 6.
# Đây là business rule vận hành, không phải taxonomy, nên nằm trong code.
CONFUSABLE_GROUPS: tuple[frozenset[str], ...] = tuple(
    frozenset(g)
    for g in [
        {"01", "02"},
        {"03", "85"},
        {"05", "06"},
        {"07", "09", "10"},
        {"19", "72", "73"},
        {"22", "36", "67"},
        {"37", "39"},
        {"43", "45"},
        {"47", "61", "62", "63", "65"},
        {"50", "52"},
        {"54", "55", "56", "59"},
        {"57", "58"},
        {"70", "86"},
        {"92", "93", "94", "95"},
        {"98", "99"},
        {"100", "60"},
        {"103", "104"},
    ]
)


@dataclass(frozen=True)
class DocumentType:
    id: str
    stt: int
    name_vi: str
    priority: int
    filename_base: str
    filename_verified: bool


class Catalog:
    def __init__(self, raw: dict, path: Path):
        self.path = path
        self.schema_version: str = raw.get("schema_version", "?")
        contract = raw.get("classification_contract") or {}
        self.allowed_type_ids: tuple[str, ...] = tuple(contract.get("allowed_type_ids") or ())
        self.fallback_type: str = contract.get("fallback_type", UNKNOWN)
        self.naming_rules: dict = raw.get("naming_rules") or {}

        types: dict[str, DocumentType] = {}
        for item in raw.get("document_types") or []:
            dt = DocumentType(
                id=str(item["id"]),
                stt=int(item["stt"]),
                name_vi=item["name_vi"],
                priority=int(item.get("priority", 9)),
                filename_base=item["filename_base"],
                filename_verified=bool(item.get("filename_example_verified_from_guidance")),
            )
            if dt.id in types:
                raise PipelineError(f"Catalog trùng type_id: {dt.id}")
            types[dt.id] = dt
        self._types = types
        self._validate()

    def _validate(self) -> None:
        if not self.allowed_type_ids:
            raise PipelineError("Catalog thiếu classification_contract.allowed_type_ids")
        missing = set(self.allowed_type_ids) - set(self._types)
        if missing:
            raise PipelineError(f"Catalog thiếu định nghĩa cho type_id: {sorted(missing)}")
        extra = set(self._types) - set(self.allowed_type_ids)
        if extra:
            raise PipelineError(f"Catalog có type_id ngoài contract: {sorted(extra)}")
        bad = [t.id for t in self._types.values() if not FILENAME_BASE_RE.match(t.filename_base)]
        if bad:
            raise PipelineError(f"filename_base không hợp lệ (phải không dấu, không khoảng trắng): {bad}")
        dupes = _duplicates([t.filename_base for t in self._types.values()])
        if dupes:
            raise PipelineError(f"filename_base bị trùng giữa các loại: {dupes}")

    # ---- truy vấn ----
    def __len__(self) -> int:
        return len(self._types)

    def __contains__(self, type_id: object) -> bool:
        return str(type_id) in self._types

    def get(self, type_id: str) -> DocumentType:
        try:
            return self._types[str(type_id)]
        except KeyError as exc:
            raise PipelineError(
                f"type_id '{type_id}' không có trong catalog. "
                f"Classifier chỉ được trả type_id trong catalog hoặc '{UNKNOWN}'."
            ) from exc

    def filename_base(self, type_id: str) -> str:
        return self.get(type_id).filename_base

    def name_vi(self, type_id: str) -> str:
        return self.get(type_id).name_vi

    def all_types(self) -> tuple[DocumentType, ...]:
        return tuple(self._types[i] for i in self.allowed_type_ids)

    def is_valid_classification(self, type_id: Optional[str]) -> bool:
        return type_id == UNKNOWN or (type_id is not None and str(type_id) in self._types)

    def confusable_group_of(self, type_id: str) -> Optional[frozenset[str]]:
        for group in CONFUSABLE_GROUPS:
            if type_id in group:
                return group
        return None

    def are_confusable(self, a: Optional[str], b: Optional[str]) -> bool:
        if not a or not b or a == b:
            return False
        group = self.confusable_group_of(a)
        return bool(group and b in group)

    def describe(self, type_ids) -> list[dict]:
        """Mô tả taxonomy gửi cho model ở second pass (AGENTS.md mục 5)."""
        out = []
        for tid in type_ids:
            if tid == UNKNOWN:
                out.append({"type_id": UNKNOWN, "name_vi": "Không xác định được loại"})
                continue
            dt = self.get(tid)
            out.append({"type_id": dt.id, "name_vi": dt.name_vi, "priority": dt.priority})
        return out


def _duplicates(values) -> list[str]:
    seen, dup = set(), set()
    for v in values:
        if v in seen:
            dup.add(v)
        seen.add(v)
    return sorted(dup)


def find_catalog_path(start: Optional[Path] = None) -> Path:
    here = Path(start or Path(__file__).resolve().parent)
    for candidate in [here, *here.parents]:
        p = candidate / CATALOG_FILENAME
        if p.is_file():
            return p
    raise PipelineError(f"Không tìm thấy {CATALOG_FILENAME}")


@lru_cache(maxsize=4)
def load_catalog(path: Optional[str] = None) -> Catalog:
    p = Path(path) if path else find_catalog_path()
    raw = json.loads(p.read_text(encoding="utf-8"))
    return Catalog(raw, p)
