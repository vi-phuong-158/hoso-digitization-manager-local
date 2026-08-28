"""Kiểu dữ liệu dùng chung giữa các phase của pipeline.

Không chứa business rule, không gọi model, không I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

UNKNOWN = "UNKNOWN"

MODE_DRY_RUN = "dry-run"
MODE_APPLY = "apply"

PageRole = Literal["CONTENT", "COVER", "BACK_SIDE", "CONTINUATION", "BLANK"]
AttachHint = Literal["PREVIOUS", "NEXT", "NONE", "UNCERTAIN"]

# Trạng thái ở trục phân loại (Phase D). Golden test đối chiếu trục này.
ClassificationStatus = Literal["AUTO", "REVIEW"]
# Trạng thái cuối cùng của logical document sau khi qua naming (Phase E).
FinalStatus = Literal["AUTO", "REVIEW"]


@dataclass(frozen=True)
class TypeCandidate:
    type_id: str
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {"type_id": self.type_id, "confidence": round(float(self.confidence), 4)}


@dataclass(frozen=True)
class PageGeometry:
    """Hình học trang, đọc trực tiếp từ PDF nguồn (không cần model)."""

    width: float
    height: float
    rotation: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": round(self.width, 1),
            "height": round(self.height, 1),
            "rotation": self.rotation,
        }


@dataclass
class PageObservation:
    """Tín hiệu Phase B cho MỘT trang nguồn.

    Đây là output của provider (model). Provider KHÔNG được trả về kết quả gom
    nhóm trang; việc gom nhóm thuộc về app.segmenter.
    """

    page_number: int  # 1-based
    page_role: PageRole = "CONTENT"
    title_guess: Optional[str] = None
    document_date: Optional[str] = None  # ISO yyyy-mm-dd
    date_confidence: float = 0.0
    type_candidates: list[TypeCandidate] = field(default_factory=list)
    starts_new_document: bool = False
    continues_previous: bool = False
    attach_hint: AttachHint = "NONE"
    attach_hint_confidence: float = 0.0
    notes: Optional[str] = None

    @property
    def is_attachment(self) -> bool:
        """Trang không thể tự đứng thành tài liệu độc lập."""
        return self.page_role in ("COVER", "BACK_SIDE", "CONTINUATION", "BLANK")

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type_candidates"] = [c.as_dict() for c in self.type_candidates]
        return d


@dataclass
class LogicalDocument:
    """Kết quả Phase C."""

    source_file: str
    source_pages: list[int]
    lead_page: int
    segmentation_confidence: float = 1.0
    segmentation_flags: list[str] = field(default_factory=list)
    page_roles: dict[int, str] = field(default_factory=dict)

    @property
    def doc_key(self) -> str:
        return f"{self.source_file}#{'-'.join(str(p) for p in self.source_pages)}"

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.segmentation_flags)


@dataclass
class DocumentClassification:
    """Kết quả Phase D (raw từ provider, chưa áp policy)."""

    type_id: str
    confidence: float
    document_date: Optional[str] = None
    date_confidence: float = 0.0
    title_short: Optional[str] = None
    runner_up: Optional[TypeCandidate] = None
    provider_note: Optional[str] = None
    # Provider/Agent tự xin REVIEW. Đây là tín hiệu MỘT CHIỀU: agent có quyền
    # kéo tài liệu sang REVIEW, nhưng không có quyền ép AUTO.
    provider_needs_review: bool = False
    provider_review_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type_id": self.type_id,
            "confidence": round(float(self.confidence), 4),
            "document_date": self.document_date,
            "date_confidence": round(float(self.date_confidence), 4),
            "title_short": self.title_short,
            "runner_up": self.runner_up.as_dict() if self.runner_up else None,
        }


@dataclass
class ClassifiedDocument:
    """Logical document + kết quả phân loại + trạng thái policy."""

    document: LogicalDocument
    classification: DocumentClassification
    classification_status: ClassificationStatus = "AUTO"
    classification_reasons: list[str] = field(default_factory=list)
    second_pass_used: bool = False
    second_pass_result: Optional[DocumentClassification] = None

    # Điền ở Phase E
    final_status: FinalStatus = "AUTO"
    final_reasons: list[str] = field(default_factory=list)
    target_file: Optional[str] = None
    target_dir: Optional[str] = None  # "output" | "review"
    sequence_index: Optional[int] = None

    # Policy metadata carried by the standard (non-incremental) manifest too.
    # Runtime analysis defaults to TAXONOMY; human-resolved state can expose
    # SUPPORTING_DOCUMENT/DUPLICATE through the incremental manifest path.
    logical_document_id: Optional[str] = None
    classification_kind: str = "TAXONOMY"
    subtype: Optional[str] = None
    date_precision: Optional[str] = None
    duplicate_of: Optional[str] = None

    @property
    def needs_review(self) -> bool:
        return self.final_status == "REVIEW"


class PipelineError(RuntimeError):
    """Lỗi nghiệp vụ của pipeline (fail-safe, không tự vá)."""
