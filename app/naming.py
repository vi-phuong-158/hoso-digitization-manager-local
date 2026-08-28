"""Phase E - Naming engine (deterministic).

AI KHÔNG được tự nghĩ filename. Tên file chỉ đến từ:
    type_id -> document_types.json -> filename_base -> sequence -> .pdf

Quy tắc (AGENTS.md mục 4 - Phase E):
- 1 tài liệu của loại  -> "<filename_base>.pdf"
- nhiều tài liệu cùng loại -> "<filename_base>.<n>.pdf", n = 1..N theo NGÀY VĂN BẢN
  từ cũ đến mới.
- Không xác định được thứ tự thời gian đủ chắc -> giữ nguyên segmentation/
  classification nhưng đưa CẢ NHÓM sang REVIEW; tuyệt đối không đánh .1/.2 theo
  thứ tự scan.

Tài liệu REVIEW không mang tên chính thức: dùng tiền tố "_REVIEW." để không thể
nhầm với tên chuẩn trong danh mục.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .catalog import Catalog
from .models import UNKNOWN, ClassifiedDocument, PipelineError
from .textnorm import slug

REVIEW_PREFIX = "_REVIEW."

# --- lý do REVIEW ở tầng naming ---
R_ORDER_NO_DATE = "ORDERING_MISSING_RELIABLE_DATE"
R_ORDER_DUPLICATE_DATE = "ORDERING_DUPLICATE_DATE"
R_PATH_TOO_LONG = "TARGET_PATH_TOO_LONG"

# Windows MAX_PATH = 260; chừa biên an toàn cho thư mục output/review.
MAX_TARGET_PATH = 240


@dataclass(frozen=True)
class NamingPolicy:
    min_date_confidence: float = 0.80
    max_target_path: int = MAX_TARGET_PATH


DEFAULT_NAMING_POLICY = NamingPolicy()


def auto_filename(catalog: Catalog, type_id: str, sequence: Optional[int] = None) -> str:
    """Tên file chính thức. Chỉ lấy filename_base từ catalog."""
    base = catalog.filename_base(type_id)
    return f"{base}.pdf" if sequence is None else f"{base}.{sequence}.pdf"


def review_filename(
    catalog: Catalog,
    type_id: Optional[str],
    source_file: str,
    pages: Sequence[int],
    max_len: int = 120,
) -> str:
    """Tên file cách ly cho ca REVIEW - truy vết được, KHÔNG phải tên chính thức.

    Cố ý KHÔNG dùng nguyên `filename_base` để không ai nhầm đây là tên chuẩn;
    chỉ giữ mã loại (ứng viên) + dấu vết nguồn/trang. Tên loại đầy đủ nằm trong
    manifest.
    """
    label = str(type_id) if (type_id and type_id != UNKNOWN and type_id in catalog) else UNKNOWN
    trace = f"{slug(Path(source_file).stem, 32)}_p{'-'.join(str(p) for p in pages)}"
    name = f"{REVIEW_PREFIX}{label}.{trace}.pdf"
    if len(name) <= max_len:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    keep = max(max_len - len(REVIEW_PREFIX) - len(label) - len(digest) - len(".pdf") - 3, 8)
    return f"{REVIEW_PREFIX}{label}.{trace[:keep]}~{digest}.pdf"


def _is_orderable(docs: Sequence[ClassifiedDocument], policy: NamingPolicy) -> list[str]:
    """Trả về danh sách lý do KHÔNG xếp được thứ tự (rỗng = xếp được)."""
    reasons: list[str] = []
    dates: list[str] = []
    for d in docs:
        c = d.classification
        if not c.document_date or c.date_confidence < policy.min_date_confidence:
            if R_ORDER_NO_DATE not in reasons:
                reasons.append(R_ORDER_NO_DATE)
        else:
            dates.append(c.document_date)
    if len(set(dates)) != len(dates) and R_ORDER_DUPLICATE_DATE not in reasons:
        reasons.append(R_ORDER_DUPLICATE_DATE)
    return reasons


def _doc_sort_key(d: ClassifiedDocument) -> tuple:
    # Chỉ dùng khi nhóm đã xác định xếp được theo ngày.
    return (d.classification.document_date or "", d.document.source_file, d.document.source_pages[0])


def assign_names(
    catalog: Catalog,
    classified: list[ClassifiedDocument],
    output_dir: Path,
    review_dir: Path,
    policy: NamingPolicy = DEFAULT_NAMING_POLICY,
) -> list[ClassifiedDocument]:
    """Gán target_file/target_dir/final_status cho từng tài liệu (sửa tại chỗ)."""
    # 1) Tách nhóm được phép đặt tên chính thức.
    pool: dict[str, list[ClassifiedDocument]] = {}
    forced_review: list[ClassifiedDocument] = []
    for d in classified:
        if d.classification_status == "AUTO" and d.classification.type_id != UNKNOWN:
            pool.setdefault(d.classification.type_id, []).append(d)
        else:
            forced_review.append(d)

    # 2) Đặt tên theo từng loại.
    for type_id, docs in pool.items():
        if len(docs) == 1:
            d = docs[0]
            d.final_status = "AUTO"
            d.sequence_index = None
            d.target_file = auto_filename(catalog, type_id)
            d.target_dir = "output"
            continue

        reasons = _is_orderable(docs, policy)
        if reasons:
            for d in docs:
                d.final_status = "REVIEW"
                d.final_reasons = sorted(set(d.final_reasons) | set(reasons))
                d.sequence_index = None
                d.target_file = review_filename(
                    catalog, type_id, d.document.source_file, d.document.source_pages
                )
                d.target_dir = "review"
            continue

        for idx, d in enumerate(sorted(docs, key=_doc_sort_key), start=1):
            d.final_status = "AUTO"
            d.sequence_index = idx
            d.target_file = auto_filename(catalog, type_id, idx)
            d.target_dir = "output"

    # 3) Ca REVIEW từ segmentation/classification.
    for d in forced_review:
        d.final_status = "REVIEW"
        d.final_reasons = sorted(set(d.final_reasons) | set(d.classification_reasons))
        d.target_file = review_filename(
            catalog, d.classification.type_id, d.document.source_file, d.document.source_pages
        )
        d.target_dir = "review"

    # 4) Bất biến: không collision, không vượt độ dài đường dẫn.
    _check_paths(classified, output_dir, review_dir, policy)
    return classified


def _check_paths(
    classified: Iterable[ClassifiedDocument],
    output_dir: Path,
    review_dir: Path,
    policy: NamingPolicy,
) -> None:
    seen: dict[str, str] = {}
    for d in classified:
        if not d.target_file:
            raise PipelineError(f"Tài liệu {d.document.doc_key} chưa được đặt tên.")
        base_dir = output_dir if d.target_dir == "output" else review_dir
        full = str(Path(base_dir) / d.target_file)
        if full in seen:
            raise PipelineError(
                f"Trùng tên file đầu ra: '{full}' cho {seen[full]} và {d.document.doc_key}."
            )
        seen[full] = d.document.doc_key
        if len(full) > policy.max_target_path:
            raise PipelineError(
                f"Đường dẫn đích quá dài ({len(full)} > {policy.max_target_path}): {full}. "
                "Hãy rút ngắn đường dẫn thư mục làm việc; không tự cắt tên trong danh mục."
            )
