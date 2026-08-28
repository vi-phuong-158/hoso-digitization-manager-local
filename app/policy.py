"""DEV POLICY CLOSURE — 4 policy phát sinh sau blind runtime test (Vi Ngọc Phương).

Thuần dữ liệu + hàm deterministic, KHÔNG gọi Vision, KHÔNG đọc PDF. Chỉ thao
tác trên dữ liệu đã có (title/notes/document_date đã được Agent trích xuất từ
trước, hoặc do người vận hành gõ tay qua `resolve-review`).

Bốn chính sách:
  1. Quyết định nhân sự (điều động/bố trí/bổ nhiệm/thăng hàm nâng lương/nghỉ
     hưu) được phép quy về type_id=87 kèm `subtype` metadata phụ - KHÔNG đổi
     tên chính thức của type 87, KHÔNG tạo type mới.
  2. Tài liệu ngoài danh mục 104 loại -> `classification_kind=SUPPORTING_DOCUMENT`
     (không phải type 105+), chỉ sau khi người vận hành xác nhận.
  3. Bản scan trùng -> `classification_kind=DUPLICATE`, giữ nguồn, không tạo
     output thứ hai. Chỉ AUTO nếu có bằng chứng deterministic rất mạnh; nghi
     ngờ mà chưa chắc -> vẫn REVIEW.
  4. Ngày văn bản có độ chính xác (DAY/MONTH/YEAR/UNKNOWN) - không fake ngày
     tháng khi tài liệu chỉ ghi năm/tháng.
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Optional

from .models import PipelineError
from .textnorm import normalize

# ---------------------------------------------------------------------------
# Policy 1 — Quyết định nhân sự -> type 87 + subtype
# ---------------------------------------------------------------------------
TYPE_ID_PERSONNEL_DECISION = "87"

SUBTYPE_TRANSFER = "transfer"
SUBTYPE_ASSIGNMENT = "assignment"
SUBTYPE_APPOINTMENT = "appointment"
SUBTYPE_PROFESSIONAL_TITLE_APPOINTMENT = "professional_title_appointment"
SUBTYPE_PROMOTION_SALARY = "promotion_salary"
SUBTYPE_RETIREMENT = "retirement"
SUBTYPE_OTHER_PERSONNEL_DECISION = "other_personnel_decision"

PERSONNEL_DECISION_SUBTYPES = (
    SUBTYPE_TRANSFER,
    SUBTYPE_ASSIGNMENT,
    SUBTYPE_APPOINTMENT,
    SUBTYPE_PROFESSIONAL_TITLE_APPOINTMENT,
    SUBTYPE_PROMOTION_SALARY,
    SUBTYPE_RETIREMENT,
    SUBTYPE_OTHER_PERSONNEL_DECISION,
)

# Ưu tiên cụm cụ thể trước cụm chung - so khớp trên text đã strip dấu/thường hoá.
_SUBTYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SUBTYPE_PROMOTION_SALARY, ("thang cap bac ham", "nang bac luong", "nang luong")),
    (SUBTYPE_RETIREMENT, ("nghi huu", "huu tri")),
    (SUBTYPE_TRANSFER, ("dieu dong",)),
    (SUBTYPE_PROFESSIONAL_TITLE_APPOINTMENT, ("bo nhiem chuc danh",)),
    (SUBTYPE_APPOINTMENT, ("bo nhiem",)),
    (SUBTYPE_ASSIGNMENT, ("bo tri",)),
)


def derive_personnel_subtype(title_short: Optional[str], notes: Optional[str] = None) -> str:
    """Suy ra subtype cho type 87 từ tiêu đề/ghi chú ĐÃ CÓ SẴN (không đọc lại PDF).

    Không khớp cụm nào -> `other_personnel_decision` (không ép nhãn sai)."""
    haystack = normalize(f"{title_short or ''} {notes or ''}")
    for subtype, keywords in _SUBTYPE_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return subtype
    return SUBTYPE_OTHER_PERSONNEL_DECISION


def is_valid_subtype(subtype: Optional[str]) -> bool:
    return subtype is None or subtype in PERSONNEL_DECISION_SUBTYPES


# ---------------------------------------------------------------------------
# Policy 2 — Tài liệu ngoài taxonomy -> SUPPORTING_DOCUMENT
# ---------------------------------------------------------------------------
CLASSIFICATION_KIND_TAXONOMY = "TAXONOMY"
CLASSIFICATION_KIND_SUPPORTING = "SUPPORTING_DOCUMENT"
CLASSIFICATION_KIND_DUPLICATE = "DUPLICATE"
CLASSIFICATION_KINDS = (
    CLASSIFICATION_KIND_TAXONOMY,
    CLASSIFICATION_KIND_SUPPORTING,
    CLASSIFICATION_KIND_DUPLICATE,
)

SUPPORTING_PREFIX = "SUPPORTING."


def _title_case_slug(text: Optional[str]) -> str:
    words = normalize(text).split()
    return "_".join(w.capitalize() for w in words if w)


def supporting_filename(title_short: Optional[str], sequence: Optional[int] = None) -> str:
    """`SUPPORTING.<Ten_tai_lieu>.pdf` hoặc `.N.pdf` nếu trùng tiêu đề.

    Deterministic, không dấu, không dùng STT 01-104 giả."""
    base = _title_case_slug(title_short)[:60].strip("_") or "Tai_lieu"
    if sequence is None:
        return f"{SUPPORTING_PREFIX}{base}.pdf"
    return f"{SUPPORTING_PREFIX}{base}.{sequence}.pdf"


def supporting_group_key(title_short: Optional[str]) -> str:
    """Khoá gom nhóm cho đánh số `.1/.2` - tiêu đề chuẩn hoá, không phụ thuộc dấu/hoa-thường."""
    return normalize(title_short) or "tai_lieu"


# ---------------------------------------------------------------------------
# Policy 3 — Duplicate page/document
# ---------------------------------------------------------------------------
DUPLICATE_STATUS_CONFIRMED = "DUPLICATE_CONFIRMED"


# ---------------------------------------------------------------------------
# Policy 4 — Partial date precision
# ---------------------------------------------------------------------------
DATE_PRECISION_DAY = "DAY"
DATE_PRECISION_MONTH = "MONTH"
DATE_PRECISION_YEAR = "YEAR"
DATE_PRECISION_UNKNOWN = "UNKNOWN"
DATE_PRECISIONS = (DATE_PRECISION_DAY, DATE_PRECISION_MONTH, DATE_PRECISION_YEAR, DATE_PRECISION_UNKNOWN)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")


def normalize_partial_date(
    value: Optional[str], precision: Optional[str] = None
) -> tuple[Optional[str], str]:
    """Validate and normalize a date together with its declared precision.

    ``None`` is represented explicitly as ``UNKNOWN``.  A supplied precision
    must agree with the ISO value; callers cannot use a precision to invent a
    missing month or day.
    """
    normalized, inferred = parse_partial_date(value)
    effective = inferred if precision is None else precision
    if effective not in DATE_PRECISIONS:
        raise PipelineError(f"date_precision không hợp lệ: {effective!r}")
    if effective != inferred:
        raise PipelineError(
            f"date_precision '{effective}' không khớp document_date {value!r}; "
            f"độ chính xác đúng là {inferred}."
        )
    return normalized, effective


def validate_classification_metadata(
    *,
    classification_kind: str,
    type_id: Optional[str],
    subtype: Optional[str],
    document_date: Optional[str],
    date_precision: Optional[str],
    duplicate_of: Optional[str],
) -> tuple[Optional[str], str]:
    """Validate policy fields shared by state and both manifest builders."""
    if classification_kind not in CLASSIFICATION_KINDS:
        raise PipelineError(f"classification_kind không hợp lệ: {classification_kind!r}")
    if classification_kind != CLASSIFICATION_KIND_TAXONOMY:
        if subtype is not None:
            raise PipelineError("subtype chỉ hợp lệ với classification_kind=TAXONOMY và type_id=87.")
        if type_id not in (None, "UNKNOWN"):
            raise PipelineError("type_id phải rỗng/UNKNOWN với tài liệu ngoài TAXONOMY.")
    if subtype is not None:
        if type_id != TYPE_ID_PERSONNEL_DECISION:
            raise PipelineError("subtype chỉ hợp lệ khi type_id = 87.")
        if not is_valid_subtype(subtype):
            raise PipelineError(f"subtype không hợp lệ: {subtype!r}")
    if classification_kind == CLASSIFICATION_KIND_DUPLICATE:
        if not duplicate_of:
            raise PipelineError("DUPLICATE phải có duplicate_of.")
    elif duplicate_of is not None:
        raise PipelineError("duplicate_of chỉ hợp lệ với classification_kind=DUPLICATE.")
    normalized_date, normalized_precision = normalize_partial_date(document_date, date_precision)
    return normalized_date, normalized_precision


def parse_partial_date(value: Optional[str]) -> tuple[Optional[str], str]:
    """(giá trị chuẩn hoá, precision). Không tự bịa ngày/tháng khi thiếu.

    Chấp nhận đúng 3 dạng: yyyy-mm-dd (DAY), yyyy-mm (MONTH), yyyy (YEAR).
    `None`/rỗng -> (None, UNKNOWN)."""
    if not value:
        return None, DATE_PRECISION_UNKNOWN
    if _DAY_RE.match(value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise PipelineError(f"document_date '{value}' không phải ngày hợp lệ.") from exc
        return value, DATE_PRECISION_DAY
    if _MONTH_RE.match(value):
        y, m = (int(p) for p in value.split("-"))
        if not (1 <= m <= 12):
            raise PipelineError(f"document_date '{value}' có tháng không hợp lệ.")
        return value, DATE_PRECISION_MONTH
    if _YEAR_RE.match(value):
        return value, DATE_PRECISION_YEAR
    raise PipelineError(
        f"document_date '{value}' sai định dạng - chỉ nhận yyyy-mm-dd / yyyy-mm / yyyy."
    )


def date_range(value: Optional[str], precision: Optional[str]) -> Optional[tuple[date, date]]:
    """Khoảng ngày [min,max] tương ứng với độ chính xác - dùng để phát hiện
    chồng lấn (ambiguous ordering) giữa các độ chính xác khác nhau."""
    if not value or precision in (None, DATE_PRECISION_UNKNOWN):
        return None
    if precision == DATE_PRECISION_DAY:
        d = date.fromisoformat(value)
        return d, d
    if precision == DATE_PRECISION_MONTH:
        y, m = (int(p) for p in value.split("-"))
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
        return first, last
    if precision == DATE_PRECISION_YEAR:
        y = int(value)
        return date(y, 1, 1), date(y, 12, 31)
    raise PipelineError(f"date_precision không hợp lệ: {precision!r}")


def ranges_overlap(a: tuple[date, date], b: tuple[date, date]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]
