"""Phase D - Document classification + chính sách confidence (AGENTS.md mục 5).

Classifier đọc TOÀN BỘ logical document (không chỉ trang đầu) thông qua provider,
sau đó áp chính sách deterministic ở đây. Confidence do model tự khai chỉ là MỘT
tín hiệu, không phải chân lý.

Ngưỡng mặc định:
  >= 0.95 và không có cờ rủi ro -> AUTO
  0.80 .. 0.9499                -> second pass bằng model
  < 0.80                        -> HUMAN REVIEW
  UNKNOWN                       -> HUMAN REVIEW
  segmentation ambiguity        -> HUMAN REVIEW
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .catalog import Catalog
from .models import (
    UNKNOWN,
    ClassifiedDocument,
    DocumentClassification,
    LogicalDocument,
    PageObservation,
    TypeCandidate,
)
from .vision_adapter import DocumentVisionProvider, validate_classification

# --- lý do đưa REVIEW ---
R_UNKNOWN = "TYPE_UNKNOWN"
R_LOW_CONF = "LOW_CONFIDENCE"
R_SECOND_PASS_LOW = "SECOND_PASS_STILL_LOW"
R_SECOND_PASS_DISAGREE = "SECOND_PASS_DISAGREES"
R_CONFUSABLE = "CONFUSABLE_TYPE_NARROW_MARGIN"
R_SEGMENTATION = "SEGMENTATION_AMBIGUITY"
R_NOT_IN_CATALOG = "TYPE_NOT_IN_CATALOG"
R_PROVIDER_FLAGGED = "AGENT_FLAGGED_REVIEW"


@dataclass(frozen=True)
class ConfidencePolicy:
    auto_threshold: float = 0.95
    second_pass_threshold: float = 0.80
    confusable_min_margin: float = 0.10


DEFAULT_POLICY = ConfidencePolicy()


def aggregate_candidates(
    observations: Sequence[PageObservation], max_candidates: int = 5
) -> list[TypeCandidate]:
    """Gộp ứng viên cấp trang thành ứng viên cấp tài liệu (chỉ để gợi ý cho model)."""
    scores: dict[str, float] = {}
    for obs in observations:
        weight = 0.5 if obs.is_attachment else 1.0
        for cand in obs.type_candidates:
            scores[cand.type_id] = max(scores.get(cand.type_id, 0.0), cand.confidence * weight)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max_candidates]
    return [TypeCandidate(tid, conf) for tid, conf in ranked]


def _risk_reasons(
    result: DocumentClassification, doc: LogicalDocument, catalog: Catalog, policy: ConfidencePolicy
) -> list[str]:
    reasons: list[str] = []
    if doc.segmentation_flags:
        reasons.append(R_SEGMENTATION)
    if result.provider_needs_review:
        # Agent chỉ có quyền KÉO sang REVIEW, không có quyền ép AUTO.
        reasons.append(R_PROVIDER_FLAGGED)
    if result.runner_up is not None and catalog.are_confusable(result.type_id, result.runner_up.type_id):
        if (result.confidence - result.runner_up.confidence) < policy.confusable_min_margin:
            reasons.append(R_CONFUSABLE)
    return reasons


def classify_document(
    provider: DocumentVisionProvider,
    catalog: Catalog,
    pdf_path: Path,
    doc: LogicalDocument,
    observations: Sequence[PageObservation],
    policy: ConfidencePolicy = DEFAULT_POLICY,
) -> ClassifiedDocument:
    candidates = aggregate_candidates(observations)
    first = provider.classify_document(pdf_path, doc.source_pages, candidates)
    first = validate_classification(first, catalog, where=f"classify {doc.doc_key}")

    reasons: list[str] = []
    second: DocumentClassification | None = None
    second_used = False
    final = first

    if first.type_id == UNKNOWN:
        reasons.append(R_UNKNOWN)
    elif first.confidence < policy.second_pass_threshold:
        reasons.append(R_LOW_CONF)
    elif first.confidence < policy.auto_threshold:
        # Second pass: model đọc lại, KHÔNG nhận kết luận vòng 1 như sự thật.
        focus_ids = _second_pass_focus(first, candidates, catalog)
        second = provider.classify_document(
            pdf_path,
            doc.source_pages,
            candidates,
            second_pass=True,
            taxonomy=catalog.describe(focus_ids),
        )
        second = validate_classification(second, catalog, where=f"classify2 {doc.doc_key}")
        second_used = True
        if second.type_id != first.type_id:
            reasons.append(R_SECOND_PASS_DISAGREE)
        elif second.confidence < policy.auto_threshold:
            reasons.append(R_SECOND_PASS_LOW)
        final = second
        if second.type_id == UNKNOWN and R_UNKNOWN not in reasons:
            reasons.append(R_UNKNOWN)

    reasons.extend(r for r in _risk_reasons(final, doc, catalog, policy) if r not in reasons)

    status = "REVIEW" if reasons else "AUTO"
    return ClassifiedDocument(
        document=doc,
        classification=final,
        classification_status=status,
        classification_reasons=sorted(set(reasons)),
        second_pass_used=second_used,
        second_pass_result=second,
    )


def _second_pass_focus(
    first: DocumentClassification, candidates: Sequence[TypeCandidate], catalog: Catalog
) -> list[str]:
    focus: list[str] = []
    for tid in [first.type_id] + [c.type_id for c in candidates]:
        if tid and tid not in focus:
            focus.append(tid)
    group = catalog.confusable_group_of(first.type_id) if first.type_id != UNKNOWN else None
    if group:
        for tid in sorted(group):
            if tid not in focus:
                focus.append(tid)
    return focus
