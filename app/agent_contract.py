"""Hợp đồng JSON giữa Antigravity Runtime Agent và pipeline local.

Agent làm phần NHẬN THỨC (đọc PDF, mô tả trang, đề xuất ranh giới tài liệu,
phân loại, đọc ngày). Code local làm phần DETERMINISTIC (validate, segment,
đặt tên, tách file, QC).

Agent gửi một file JSON cho mỗi PDF nguồn:

    analysis/<person_folder>/<pdf_stem>.json

Agent KHÔNG được gửi:
  - tên file đầu ra (target_file/filename/output_name...),
  - số thứ tự .1/.2,
  - trạng thái AUTO,
  - type_id ngoài danh mục.

Validator dưới đây là hàng rào cứng: JSON sai một điểm là DỪNG, không đoán,
không tự sửa hộ Agent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .catalog import Catalog
from .models import UNKNOWN, DocumentClassification, PageObservation, PipelineError, TypeCandidate
from .vision_adapter import VALID_HINTS, VALID_ROLES

ANALYSIS_DIRNAME = "analysis"
SUPPORTED_SCHEMA_VERSIONS = ("1.0",)
MAX_TITLE_LEN = 200
MAX_NOTE_LEN = 300

# Agent tuyệt đối không được gửi tên file. Bất kỳ khóa nào dạng này là vi phạm.
FORBIDDEN_DOC_KEYS = (
    "target_file",
    "target_dir",
    "filename",
    "file_name",
    "output_file",
    "output_name",
    "sequence",
    "status",
)


class AnalysisContractError(PipelineError):
    """Agent trả JSON không đúng hợp đồng."""


@dataclass
class AgentDocument:
    source_pages: list[int]
    type_id: str
    confidence: float
    document_date: Optional[str]
    date_confidence: float
    title_short: Optional[str]
    needs_review: bool
    review_reason: Optional[str]


@dataclass
class AgentAnalysis:
    schema_version: str
    person_folder: str
    source_file: str
    page_count: int
    pages: list[PageObservation]
    documents: list[AgentDocument]
    produced_by: Optional[str] = None
    raw_path: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)

    def document_for(self, pages: list[int]) -> Optional[AgentDocument]:
        want = list(pages)
        for d in self.documents:
            if d.source_pages == want:
                return d
        return None

    def proposed_groups(self) -> list[list[int]]:
        return [list(d.source_pages) for d in self.documents]


# --------------------------------------------------------------------------
# Kiểm tra kiểu cơ bản
# --------------------------------------------------------------------------
def _fail(where: str, msg: str) -> "AnalysisContractError":
    return AnalysisContractError(f"{where}: {msg}")


def _need(obj: dict, key: str, where: str) -> Any:
    if key not in obj:
        raise _fail(where, f"thiếu trường bắt buộc '{key}'")
    return obj[key]


def _as_confidence(value: Any, where: str, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(where, f"'{key}' phải là số 0..1, nhận {value!r}")
    f = float(value)
    if not (0.0 <= f <= 1.0):
        raise _fail(where, f"'{key}' ngoài khoảng [0,1]: {f}")
    return f


def _as_date(value: Any, where: str, key: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(where, f"'{key}' phải là chuỗi yyyy-mm-dd hoặc null, nhận {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _fail(where, f"'{key}' không phải ngày yyyy-mm-dd hợp lệ: {value!r}") from exc
    return parsed.isoformat()


def _as_text(value: Any, where: str, key: str, max_len: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(where, f"'{key}' phải là chuỗi hoặc null")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_len:
        raise _fail(where, f"'{key}' quá dài ({len(text)} > {max_len}) - không chép toàn văn tài liệu")
    return text


def _as_bool(value: Any, where: str, key: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _fail(where, f"'{key}' phải là true/false, nhận {value!r}")
    return value


def _as_type_id(value: Any, catalog: Catalog, where: str) -> str:
    if not isinstance(value, str):
        raise _fail(where, f"'type_id' phải là chuỗi, nhận {value!r}")
    tid = value.strip()
    if not catalog.is_valid_classification(tid):
        raise _fail(
            where,
            f"type_id '{tid}' không có trong document_types.json. "
            f"Chỉ được dùng 01-104 hoặc '{UNKNOWN}'. Agent không được tự tạo loại mới.",
        )
    return tid


# --------------------------------------------------------------------------
# Parse + validate
# --------------------------------------------------------------------------
def parse_analysis(raw_text: str, catalog: Catalog, where: str = "analysis") -> AgentAnalysis:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise _fail(where, f"JSON hỏng: {exc}") from exc
    if not isinstance(data, dict):
        raise _fail(where, "JSON gốc phải là một object")

    version = str(_need(data, "schema_version", where))
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _fail(where, f"schema_version '{version}' không được hỗ trợ {SUPPORTED_SCHEMA_VERSIONS}")

    source_file = str(_need(data, "source_file", where))
    person_folder = str(_need(data, "person_folder", where))
    page_count = _need(data, "page_count", where)
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise _fail(where, f"'page_count' phải là số nguyên >= 1, nhận {page_count!r}")

    pages = _parse_pages(data, catalog, page_count, where)
    documents = _parse_documents(data, catalog, page_count, where)

    return AgentAnalysis(
        schema_version=version,
        person_folder=person_folder,
        source_file=source_file,
        page_count=page_count,
        pages=pages,
        documents=documents,
        produced_by=data.get("produced_by"),
    )


def _parse_pages(
    data: dict, catalog: Catalog, page_count: int, where: str
) -> list[PageObservation]:
    rows = _need(data, "pages", where)
    if not isinstance(rows, list) or not rows:
        raise _fail(where, "'pages' phải là mảng không rỗng - Agent phải đọc ĐỦ mọi trang")

    seen: dict[int, PageObservation] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise _fail(where, "mỗi phần tử 'pages' phải là object")
        num = _need(row, "page_number", where)
        if isinstance(num, bool) or not isinstance(num, int):
            raise _fail(where, f"'page_number' phải là số nguyên, nhận {num!r}")
        w = f"{where}/trang {num}"
        if not (1 <= num <= page_count):
            raise _fail(w, f"page_number ngoài phạm vi 1..{page_count}")
        if num in seen:
            raise _fail(w, "page_number bị lặp trong 'pages'")

        role = str(row.get("page_role") or "CONTENT").upper()
        if role not in VALID_ROLES:
            raise _fail(w, f"page_role '{role}' không hợp lệ, phải thuộc {VALID_ROLES}")
        hint = str(row.get("attach_hint") or "NONE").upper()
        if hint not in VALID_HINTS:
            raise _fail(w, f"attach_hint '{hint}' không hợp lệ, phải thuộc {VALID_HINTS}")

        candidates_raw = row.get("type_candidates") or []
        if not isinstance(candidates_raw, list):
            raise _fail(w, "'type_candidates' phải là mảng")
        candidates: list[TypeCandidate] = []
        for c in candidates_raw:
            if not isinstance(c, dict):
                raise _fail(w, "mỗi ứng viên phải là object {type_id, confidence}")
            candidates.append(
                TypeCandidate(
                    _as_type_id(_need(c, "type_id", w), catalog, w),
                    _as_confidence(_need(c, "confidence", w), w, "confidence"),
                )
            )

        seen[num] = PageObservation(
            page_number=num,
            page_role=role,  # type: ignore[arg-type]
            title_guess=_as_text(row.get("title_guess"), w, "title_guess", MAX_TITLE_LEN),
            document_date=_as_date(row.get("document_date"), w, "document_date"),
            date_confidence=_as_confidence(row.get("date_confidence") or 0.0, w, "date_confidence"),
            type_candidates=candidates,
            starts_new_document=_as_bool(row.get("starts_new_document"), w, "starts_new_document"),
            continues_previous=_as_bool(row.get("continues_previous"), w, "continues_previous"),
            attach_hint=hint,  # type: ignore[arg-type]
            attach_hint_confidence=_as_confidence(
                row.get("attach_hint_confidence") or 0.0, w, "attach_hint_confidence"
            ),
            notes=_as_text(row.get("notes"), w, "notes", MAX_NOTE_LEN),
        )

    missing = sorted(set(range(1, page_count + 1)) - set(seen))
    if missing:
        raise _fail(where, f"Agent bỏ sót trang trong 'pages': {missing}")
    return [seen[p] for p in sorted(seen)]


def _parse_documents(
    data: dict, catalog: Catalog, page_count: int, where: str
) -> list[AgentDocument]:
    rows = _need(data, "documents", where)
    if not isinstance(rows, list) or not rows:
        raise _fail(where, "'documents' phải là mảng không rỗng")

    docs: list[AgentDocument] = []
    owner: dict[int, str] = {}
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise _fail(where, f"documents[{idx}] phải là object")
        bad_keys = sorted(
            k for k in row if any(f in str(k).lower() for f in FORBIDDEN_DOC_KEYS)
        )
        if bad_keys:
            raise _fail(
                f"{where}/documents[{idx}]",
                f"Agent không được gửi {bad_keys}. Tên file do naming engine local sinh ra.",
            )

        pages = _need(row, "source_pages", where)
        w = f"{where}/documents[{idx}] trang {pages}"
        if not isinstance(pages, list) or not pages:
            raise _fail(w, "'source_pages' phải là mảng không rỗng (không có logical document rỗng)")
        if not all(isinstance(p, int) and not isinstance(p, bool) for p in pages):
            raise _fail(w, "'source_pages' chỉ chứa số nguyên")
        out_of_range = [p for p in pages if not (1 <= p <= page_count)]
        if out_of_range:
            raise _fail(w, f"trang ngoài phạm vi 1..{page_count}: {out_of_range}")
        if len(set(pages)) != len(pages):
            raise _fail(w, "trang bị lặp trong cùng một logical document")
        if pages != sorted(pages):
            raise _fail(w, "'source_pages' phải theo đúng thứ tự trang gốc, không được đảo")
        for p in pages:
            if p in owner:
                raise _fail(w, f"trang {p} đã thuộc logical document {owner[p]} (overlap)")
            owner[p] = str(pages)

        docs.append(
            AgentDocument(
                source_pages=list(pages),
                type_id=_as_type_id(_need(row, "type_id", w), catalog, w),
                confidence=_as_confidence(_need(row, "confidence", w), w, "confidence"),
                document_date=_as_date(row.get("document_date"), w, "document_date"),
                date_confidence=_as_confidence(row.get("date_confidence") or 0.0, w, "date_confidence"),
                title_short=_as_text(row.get("title_short"), w, "title_short", MAX_TITLE_LEN),
                needs_review=_as_bool(row.get("needs_review"), w, "needs_review"),
                review_reason=_as_text(row.get("review_reason"), w, "review_reason", MAX_NOTE_LEN),
            )
        )

    missing = sorted(set(range(1, page_count + 1)) - set(owner))
    if missing:
        raise _fail(where, f"trang chưa thuộc logical document nào: {missing}")
    return docs


def load_analysis(
    path: Path, catalog: Catalog, *, expect_source: Optional[str] = None, expect_pages: Optional[int] = None
) -> AgentAnalysis:
    path = Path(path)
    if not path.is_file():
        raise AnalysisContractError(
            f"Chưa có file phân tích của Agent: {path}\n"
            f"Antigravity Runtime Agent phải đọc PDF và ghi JSON theo hợp đồng "
            f"(xem .agents/rules/party-record-digitization.md) trước khi chạy pipeline."
        )
    analysis = parse_analysis(path.read_text(encoding="utf-8"), catalog, where=path.name)
    analysis.raw_path = path
    if expect_source and analysis.source_file != expect_source:
        raise _fail(
            path.name,
            f"'source_file' là {analysis.source_file!r} nhưng đang xử lý {expect_source!r}",
        )
    if expect_pages is not None and analysis.page_count != expect_pages:
        raise _fail(
            path.name,
            f"'page_count' là {analysis.page_count} nhưng PDF thật có {expect_pages} trang",
        )
    return analysis


def to_classification(doc: AgentDocument) -> DocumentClassification:
    return DocumentClassification(
        type_id=doc.type_id,
        confidence=doc.confidence,
        document_date=doc.document_date,
        date_confidence=doc.date_confidence,
        title_short=doc.title_short,
        provider_note="antigravity-agent",
        provider_needs_review=doc.needs_review,
        provider_review_reason=doc.review_reason,
    )
