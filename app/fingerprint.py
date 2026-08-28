"""Fingerprint cho analysis cache (Phase C).

Một bản phân tích (logical_documents đã lưu trong state DB) chỉ được TÁI SỬ
DỤNG khi cả ba thành phần dưới đây còn khớp với lần phân tích gốc:

  - source_sha256          (nội dung PDF không đổi — đã có sẵn ở tầng inventory)
  - taxonomy_version       (document_types.json không đổi)
  - analysis_schema_version (hợp đồng JSON Agent<->code không đổi)

pipeline_version cũng được lưu để tham khảo/audit, nhưng KHÔNG bắt buộc khớp
tuyệt đối để coi cache hợp lệ — một bản vá nhỏ trong code local (vd sửa lỗi in
ấn) không nhất thiết làm invalid hoá phân tích của Agent. Taxonomy và schema
hợp đồng mới là thứ ảnh hưởng trực tiếp tới KẾT QUẢ phân loại.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import __version__ as PIPELINE_VERSION
from .agent_contract import SUPPORTED_SCHEMA_VERSIONS
from .catalog import Catalog

ANALYSIS_SCHEMA_VERSION = SUPPORTED_SCHEMA_VERSIONS[-1]


@dataclass(frozen=True)
class Fingerprint:
    taxonomy_version: str
    analysis_schema_version: str
    pipeline_version: str

    def matches_cache(self, taxonomy_version: str, analysis_schema_version: str) -> bool:
        """So khớp phần BẮT BUỘC (taxonomy + schema). pipeline_version không xét."""
        return (
            self.taxonomy_version == taxonomy_version
            and self.analysis_schema_version == analysis_schema_version
        )


def taxonomy_version_of(catalog: Catalog) -> str:
    return hashlib.sha256(Path(catalog.path).read_bytes()).hexdigest()


def current_fingerprint(catalog: Catalog) -> Fingerprint:
    return Fingerprint(
        taxonomy_version=taxonomy_version_of(catalog),
        analysis_schema_version=ANALYSIS_SCHEMA_VERSION,
        pipeline_version=PIPELINE_VERSION,
    )
