"""Rehearsal cục bộ cho DEV POLICY CLOSURE (mục 21).

CHỈ đọc dữ liệu ĐÃ ĐÓNG BĂNG (manifest dry-run có sẵn + analysis JSON đã
freeze) — KHÔNG mở PDF, KHÔNG gọi Vision, KHÔNG mutate state DB, KHÔNG
resolve-review thật, KHÔNG apply. Mục đích DUY NHẤT: tính trước AUTO/REVIEW
sẽ đổi ra sao nếu áp 2 policy CHẮC CHẮN deterministic (type 87 + subtype,
partial-date đọc từ notes đã có sẵn) — Supporting/Duplicate KHÔNG BAO GIỜ tự
resolve ở đây vì cần bằng chứng/xác nhận của người vận hành (Policy 2/3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .policy import TYPE_ID_PERSONNEL_DECISION, derive_personnel_subtype, parse_partial_date

_MONTH_YEAR_IN_NOTES = re.compile(r"th[aá]ng\s+(\d{1,2})\s+n[ăa]m\s+(\d{4})", re.IGNORECASE)


def extract_month_year_from_notes(notes: Optional[str]) -> Optional[str]:
    """Suy 'yyyy-mm' từ một ghi chú Agent ĐÃ VIẾT SẴN lúc phân tích (không đọc
    lại PDF) — vd "Ngày để trống (chỉ ghi tháng 11 năm 2023)". Regex xác định,
    không suy diễn ngày. None nếu không khớp mẫu chắc chắn."""
    if not notes:
        return None
    m = _MONTH_YEAR_IN_NOTES.search(notes)
    if not m:
        return None
    month, year = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12):
        return None
    return f"{year:04d}-{month:02d}"


def notes_for_document(raw_analyses: list[dict], source_file: str, source_pages: list[int]) -> Optional[str]:
    """Ghép notes cấp trang (trong analysis JSON gốc) của các trang thuộc một
    logical document — chỉ đọc dữ liệu Agent đã ghi từ trước."""
    for raw in raw_analyses:
        if raw.get("source_file") != source_file:
            continue
        pages_by_no = {p["page_number"]: p for p in raw.get("pages", [])}
        parts = [
            pages_by_no[p]["notes"]
            for p in source_pages
            if p in pages_by_no and pages_by_no[p].get("notes")
        ]
        return " | ".join(parts) if parts else None
    return None


@dataclass(frozen=True)
class RehearsalResolution:
    logical_document_id: str
    source_file: str
    source_pages: list
    reason: str  # "TYPE_87_SUBTYPE" | "PARTIAL_DATE_FROM_NOTES"
    title_short: Optional[str] = None
    resolved_type_id: Optional[str] = None
    resolved_subtype: Optional[str] = None
    resolved_document_date: Optional[str] = None
    resolved_date_precision: Optional[str] = None


def deterministic_resolutions(
    documents: list[dict], raw_analyses: Optional[list[dict]] = None
) -> list[RehearsalResolution]:
    """Chỉ 2 policy KHÔNG mơ hồ (Policy 1, Policy 4-khi-notes-đã-rõ-ràng).

    KHÔNG BAO GIỜ tự resolve SUPPORTING_DOCUMENT hay DUPLICATE ở đây — hai cái
    đó bắt buộc người vận hành xác nhận (Policy 2/3), đúng mục 21: "Không tự
    resolve supporting/duplicate nếu chưa human-confirmed."
    """
    raw_analyses = raw_analyses or []
    out: list[RehearsalResolution] = []
    for doc in documents:
        if not doc.get("needs_review"):
            continue
        reasons = set(doc.get("review_reason") or [])

        if doc.get("type_id") == TYPE_ID_PERSONNEL_DECISION and reasons and reasons <= {
            "LOW_CONFIDENCE", "AGENT_FLAGGED_REVIEW", "SECOND_PASS_STILL_LOW",
        }:
            out.append(
                RehearsalResolution(
                    logical_document_id=doc["logical_document_id"],
                    source_file=doc["source_file"],
                    source_pages=list(doc["source_pages"]),
                    reason="TYPE_87_SUBTYPE",
                    title_short=doc.get("title_short"),
                    resolved_type_id=TYPE_ID_PERSONNEL_DECISION,
                    resolved_subtype=derive_personnel_subtype(doc.get("title_short")),
                )
            )
            continue

        if (
            doc.get("type_id") not in (None, "UNKNOWN")
            and reasons == {"AGENT_FLAGGED_REVIEW"}
            and not doc.get("document_date")
        ):
            notes = notes_for_document(raw_analyses, doc["source_file"], doc["source_pages"])
            month_year = extract_month_year_from_notes(notes)
            if month_year:
                normalized, precision = parse_partial_date(month_year)
                out.append(
                    RehearsalResolution(
                        logical_document_id=doc["logical_document_id"],
                        source_file=doc["source_file"],
                        source_pages=list(doc["source_pages"]),
                        reason="PARTIAL_DATE_FROM_NOTES",
                        title_short=doc.get("title_short"),
                        resolved_document_date=normalized,
                        resolved_date_precision=precision,
                    )
                )
    return out


@dataclass(frozen=True)
class RehearsalReport:
    auto_before: int
    review_before: int
    auto_after: int
    review_after: int
    resolutions: list[RehearsalResolution]
    remaining_review: list[dict] = field(default_factory=list)

    def summary_text(self) -> str:
        lines = [
            f"BEFORE: AUTO {self.auto_before}  REVIEW {self.review_before}",
            f"AFTER : AUTO {self.auto_after}  REVIEW {self.review_after}",
            "",
            f"Resolve tự động ({len(self.resolutions)}):",
        ]
        for r in self.resolutions:
            extra = (
                f"type_id=87 subtype={r.resolved_subtype}"
                if r.reason == "TYPE_87_SUBTYPE"
                else f"date={r.resolved_document_date} precision={r.resolved_date_precision}"
            )
            lines.append(f"  [{r.reason}] {r.source_file} {r.source_pages} {r.title_short!r} -> {extra}")
        lines.append("")
        lines.append(f"Còn REVIEW (cần người vận hành - supporting/duplicate/khác) ({len(self.remaining_review)}):")
        for d in self.remaining_review:
            lines.append(
                f"  {d['source_file']} {d['source_pages']} [{d.get('type_id')}] "
                f"{d.get('title_short')!r} lý do={d.get('review_reason')}"
            )
        return "\n".join(lines)


def rehearse(manifest: dict, raw_analyses: Optional[list[dict]] = None) -> RehearsalReport:
    """`manifest`: một manifest dry-run ĐÃ CÓ SẴN trên đĩa (schema
    `_incremental_manifest`). `raw_analyses`: danh sách analysis JSON gốc đã
    freeze (để đọc `notes` cấp trang cho Policy 4). KHÔNG đọc gì khác."""
    docs = manifest.get("documents") or []
    summary = manifest.get("summary") or {}
    auto_before = int(summary.get("auto_resolved", 0))
    review_before = sum(1 for d in docs if d.get("needs_review"))
    resolutions = deterministic_resolutions(docs, raw_analyses)
    resolved_ids = {r.logical_document_id for r in resolutions}
    remaining_review = [
        d for d in docs if d.get("needs_review") and d["logical_document_id"] not in resolved_ids
    ]
    return RehearsalReport(
        auto_before=auto_before,
        review_before=review_before,
        auto_after=auto_before + len(resolutions),
        review_after=review_before - len(resolutions),
        resolutions=resolutions,
        remaining_review=remaining_review,
    )
