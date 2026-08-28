"""Phase E — Human review resolution.

Người vận hành chốt một logical document đang REVIEW_PENDING mà KHÔNG cần
Agent đọc lại toàn bộ PDF nguồn. Sau khi resolve, lượt `apply` kế tiếp sẽ tính
lại global naming (app/global_naming.py) và ghi file thật.

DEV POLICY CLOSURE bổ sung 3 kiểu chốt, ngoài kiểu TAXONOMY gốc:
  - SUPPORTING_DOCUMENT: tài liệu ngoài danh mục 104 loại (`--supporting`).
  - DUPLICATE: bản scan trùng, không tạo output riêng (`--duplicate-of`).
  - Chỉ bổ sung/sửa ngày với độ chính xác (`--date` + `--date-precision`),
    giữ nguyên type_id gốc.
Không kiểu nào được tự động chọn thay người vận hành - lời gọi luôn tường
minh, đến từ CLI/operator, không phải do Agent tự suy diễn.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .catalog import Catalog
from .models import PipelineError, UNKNOWN
from .policy import (
    CLASSIFICATION_KIND_DUPLICATE,
    CLASSIFICATION_KIND_SUPPORTING,
    CLASSIFICATION_KIND_TAXONOMY,
    is_valid_subtype,
    parse_partial_date,
    validate_classification_metadata,
)
from .state import LogicalDocumentRow, StateRegistry


@dataclass(frozen=True)
class ReviewItem:
    logical_document_id: str
    source_filename: str
    source_pages: list[int]
    type_id: str
    confidence: float
    document_date: Optional[str]
    title_short: Optional[str]
    classification_reasons: list[str]


def list_pending_reviews(registry: StateRegistry, person_folder: str) -> list[ReviewItem]:
    out = []
    for row in registry.logical_documents_for_person(person_folder, include_retired=False):
        if row.resolution_status != "REVIEW_PENDING":
            continue
        src = registry.get(row.source_hash)
        out.append(
            ReviewItem(
                logical_document_id=row.logical_document_id,
                source_filename=src.source_filename if src else row.source_hash[:12],
                source_pages=list(row.source_pages),
                type_id=row.type_id,
                confidence=row.confidence,
                document_date=row.document_date,
                title_short=row.title_short,
                classification_reasons=list(row.classification_reasons),
            )
        )
    return out


def resolve_review(
    registry: StateRegistry,
    catalog: Catalog,
    logical_document_id: str,
    *,
    type_id: Optional[str] = None,
    subtype: Optional[str] = None,
    supporting: bool = False,
    duplicate_of: Optional[str] = None,
    document_date: Optional[str] = None,
    date_precision: Optional[str] = None,
    resolved_by: str = "operator",
) -> LogicalDocumentRow:
    """Validate rồi ghi quyết định của người vận hành. KHÔNG tự động ghi file.

    Đúng MỘT trong ba: `type_id` (TAXONOMY, có thể kèm `subtype`), `supporting=True`
    (SUPPORTING_DOCUMENT), hoặc `duplicate_of` (DUPLICATE) - hoặc KHÔNG cái nào
    nếu chỉ đơn thuần bổ sung/sửa ngày cho một tài liệu TAXONOMY đã đúng loại.
    """
    chosen = [bool(type_id), supporting, bool(duplicate_of)]
    if sum(chosen) > 1:
        raise PipelineError(
            "resolve-review: chỉ được chọn MỘT trong --type-id / --supporting / --duplicate-of."
        )

    kind = CLASSIFICATION_KIND_TAXONOMY
    resolved_type_id: Optional[str] = None
    resolved_subtype: Optional[str] = None
    resolved_duplicate_of: Optional[str] = None

    if type_id:
        if not catalog.is_valid_classification(type_id) or type_id == UNKNOWN:
            raise PipelineError(
                f"resolve-review: type_id '{type_id}' không hợp lệ (phải trong danh mục, khác UNKNOWN)."
            )
        if subtype is not None and type_id != "87":
            raise PipelineError("resolve-review: subtype chỉ hợp lệ khi --type-id 87.")
        if not is_valid_subtype(subtype):
            raise PipelineError(f"resolve-review: subtype '{subtype}' không hợp lệ.")
        resolved_type_id = type_id
        resolved_subtype = subtype
        kind = CLASSIFICATION_KIND_TAXONOMY
    elif supporting:
        if subtype:
            raise PipelineError("resolve-review: --subtype chỉ dùng cùng --type-id (TAXONOMY).")
        kind = CLASSIFICATION_KIND_SUPPORTING
    elif duplicate_of:
        if duplicate_of == logical_document_id:
            raise PipelineError("resolve-review: một tài liệu không thể trùng chính nó.")
        target = registry.get_logical_document(duplicate_of)
        if target is None:
            raise PipelineError(f"resolve-review: duplicate_of '{duplicate_of}' không tồn tại.")
        if target.effective_classification_kind == CLASSIFICATION_KIND_DUPLICATE:
            raise PipelineError(
                "resolve-review: duplicate_of phải trỏ tới bản GỐC, không phải một DUPLICATE khác "
                "(không cho phép chuỗi trùng lặp)."
            )
        resolved_duplicate_of = duplicate_of
        kind = CLASSIFICATION_KIND_DUPLICATE
    else:
        # Không đổi kind/type_id - chỉ dùng khi mục đích DUY NHẤT là bổ sung/sửa ngày
        # cho một tài liệu TAXONOMY mà type_id gốc đã đúng (vd Phiếu bổ sung thiếu ngày).
        if document_date is None:
            raise PipelineError(
                "resolve-review: phải cung cấp ít nhất một trong --type-id / --supporting / "
                "--duplicate-of / --date."
            )
        resolved_type_id = None  # effective_type_id giữ nguyên type_id gốc

    if document_date is None and date_precision not in (None, "UNKNOWN"):
        raise PipelineError(
            "resolve-review: date_precision phải là UNKNOWN khi --date bị bỏ trống."
        )
    resolved_date_precision = "UNKNOWN" if date_precision == "UNKNOWN" else None
    if document_date is not None:
        normalized, inferred_precision = parse_partial_date(document_date)
        if date_precision is not None and date_precision != inferred_precision:
            raise PipelineError(
                f"resolve-review: --date-precision '{date_precision}' không khớp định dạng "
                f"của --date '{document_date}' (suy ra {inferred_precision}). Không tự bịa "
                "ngày/tháng - chỉ ghi đúng độ chính xác đọc được."
            )
        resolved_date_precision = inferred_precision
        document_date = normalized

    validate_classification_metadata(
        classification_kind=kind,
        type_id=resolved_type_id,
        subtype=resolved_subtype,
        document_date=document_date,
        date_precision=resolved_date_precision,
        duplicate_of=resolved_duplicate_of,
    )

    registry.resolve_review(
        logical_document_id,
        resolved_classification_kind=kind,
        resolved_type_id=resolved_type_id,
        resolved_subtype=resolved_subtype,
        resolved_document_date=document_date,
        resolved_date_precision=resolved_date_precision,
        duplicate_of=resolved_duplicate_of,
        resolved_by=resolved_by,
    )
    row = registry.get_logical_document(logical_document_id)
    assert row is not None
    return row
