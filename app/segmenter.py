"""Phase C - Document segmentation.

Gom các trang nguồn thành logical_document. Đây là logic deterministic của hệ
thống: provider (model) chỉ cung cấp tín hiệu cấp TRANG, việc quyết định ranh
giới tài liệu nằm ở đây.

Bằng chứng dùng để ghép bìa/mặt sau (AGENTS.md mục 7):
  1. tương đồng tiêu đề bìa <-> tiêu đề trang nội dung;
  2. tương đồng khổ trang (đọc thẳng từ PDF, không cần model);
  3. gợi ý hướng ghép của model (attach_hint).

Bất định -> REVIEW, không đoán.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import LogicalDocument, PageObservation, PipelineError
from .pdf_inventory import SourceFile, geometry_similarity
from .textnorm import title_similarity

# --- cờ segmentation (đưa tài liệu sang REVIEW) ---
FLAG_AMBIGUOUS_COVER = "AMBIGUOUS_COVER_BINDING"
FLAG_ORPHAN_ATTACHMENT = "ORPHAN_ATTACHMENT"
FLAG_WEAK_BOUNDARY = "WEAK_DOCUMENT_BOUNDARY"
FLAG_BACKSIDE_MISMATCH = "BACKSIDE_GEOMETRY_MISMATCH"
FLAG_CONTINUATION_NO_PARENT = "CONTINUATION_WITHOUT_PARENT"


@dataclass(frozen=True)
class SegmentationConfig:
    w_title: float = 0.45
    w_geometry: float = 0.30
    w_hint: float = 0.25
    min_attach_score: float = 0.35
    min_attach_margin: float = 0.10
    min_backside_geometry: float = 0.50


DEFAULT_SEGMENTATION_CONFIG = SegmentationConfig()


@dataclass
class _Doc:
    lead_page: int
    pages: list[int]
    flags: list[str]
    confidence: float = 1.0

    def add(self, page: int) -> None:
        self.pages.append(page)
        self.pages.sort()


def _validate_observations(
    source: SourceFile, observations: list[PageObservation]
) -> dict[int, PageObservation]:
    by_page: dict[int, PageObservation] = {}
    for obs in observations:
        if obs.page_number in by_page:
            raise PipelineError(f"{source.name}: trang {obs.page_number} có 2 observation.")
        by_page[obs.page_number] = obs
    expected = set(range(1, source.pages + 1))
    got = set(by_page)
    if got != expected:
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        raise PipelineError(
            f"{source.name}: observation không phủ hết trang. thiếu={missing} thừa={extra}"
        )
    return by_page


def _hint_score(obs: PageObservation, direction: str) -> float:
    if obs.attach_hint == direction:
        return max(0.0, min(1.0, obs.attach_hint_confidence or 1.0))
    return 0.0


def _attach_score(
    cfg: SegmentationConfig,
    obs: PageObservation,
    neighbour_title: Optional[str],
    geo_sim: float,
    direction: str,
) -> float:
    t = title_similarity(obs.title_guess, neighbour_title)
    return cfg.w_title * t + cfg.w_geometry * geo_sim + cfg.w_hint * _hint_score(obs, direction)


def segment_source(
    source: SourceFile,
    observations: list[PageObservation],
    config: SegmentationConfig = DEFAULT_SEGMENTATION_CONFIG,
) -> list[LogicalDocument]:
    by_page = _validate_observations(source, observations)
    n = source.pages

    # 1) Anchor = trang nội dung mở đầu một tài liệu.
    anchor_pages: list[int] = []
    for p in range(1, n + 1):
        obs = by_page[p]
        if obs.is_attachment or obs.continues_previous:
            continue
        anchor_pages.append(p)

    docs: dict[int, _Doc] = {}
    for p in anchor_pages:
        obs = by_page[p]
        flags: list[str] = []
        if not obs.starts_new_document:
            # Trang nội dung không tự nhận là mở đầu, cũng không nói tiếp trang trước.
            flags.append(FLAG_WEAK_BOUNDARY)
        docs[p] = _Doc(lead_page=p, pages=[p], flags=flags)

    owner: dict[int, int] = {p: p for p in anchor_pages}  # page -> lead_page

    def doc_title(lead: int) -> Optional[str]:
        return by_page[lead].title_guess

    # 2) Ghép các trang phụ thuộc, đi theo đúng thứ tự trang.
    orphans: list[int] = []
    for p in range(1, n + 1):
        obs = by_page[p]
        if p in owner:
            continue

        prev_lead = owner.get(p - 1)
        next_lead = p + 1 if (p + 1) in docs else None

        is_tail = obs.page_role in ("BACK_SIDE", "CONTINUATION") or obs.continues_previous
        if is_tail:
            if prev_lead is None:
                orphans.append(p)
                continue
            geo = geometry_similarity(source.geometry.get(p), source.geometry.get(p - 1))
            docs[prev_lead].add(p)
            owner[p] = prev_lead
            if obs.page_role == "BACK_SIDE" and geo < config.min_backside_geometry:
                docs[prev_lead].flags.append(FLAG_BACKSIDE_MISMATCH)
            continue

        # COVER / BLANK: có thể thuộc trang trước hoặc trang sau.
        score_prev = -1.0
        if prev_lead is not None:
            score_prev = _attach_score(
                config,
                obs,
                doc_title(prev_lead),
                geometry_similarity(source.geometry.get(p), source.geometry.get(p - 1)),
                "PREVIOUS",
            )
        score_next = -1.0
        if next_lead is not None:
            score_next = _attach_score(
                config,
                obs,
                doc_title(next_lead),
                geometry_similarity(source.geometry.get(p), source.geometry.get(p + 1)),
                "NEXT",
            )

        if score_prev < 0 and score_next < 0:
            orphans.append(p)
            continue

        if score_prev >= score_next:
            winner, win_score, loser_score = prev_lead, score_prev, score_next
        else:
            winner, win_score, loser_score = next_lead, score_next, score_prev

        margin = win_score - loser_score if loser_score >= 0 else win_score

        if win_score < config.min_attach_score or (
            loser_score >= 0 and margin < config.min_attach_margin
        ):
            # Không chắc bìa thuộc trước hay sau -> REVIEW (AGENTS.md mục 7).
            docs[p] = _Doc(
                lead_page=p,
                pages=[p],
                flags=[FLAG_AMBIGUOUS_COVER],
                confidence=round(max(win_score, 0.0), 4),
            )
            owner[p] = p
            continue

        assert winner is not None
        docs[winner].add(p)
        owner[p] = winner
        docs[winner].confidence = min(docs[winner].confidence, round(win_score, 4))

    # 3) Trang mồ côi vẫn phải được kể tới -> tài liệu riêng, cờ REVIEW.
    for p in orphans:
        obs = by_page[p]
        flag = FLAG_CONTINUATION_NO_PARENT if obs.continues_previous else FLAG_ORPHAN_ATTACHMENT
        docs[p] = _Doc(lead_page=p, pages=[p], flags=[flag], confidence=0.0)
        owner[p] = p

    # 4) Kết xuất theo thứ tự trang; KHÔNG đổi thứ tự trang bên trong tài liệu.
    result: list[LogicalDocument] = []
    for lead in sorted(docs):
        d = docs[lead]
        result.append(
            LogicalDocument(
                source_file=source.name,
                source_pages=sorted(d.pages),
                lead_page=d.lead_page,
                segmentation_confidence=d.confidence,
                segmentation_flags=sorted(set(d.flags)),
                page_roles={p: by_page[p].page_role for p in sorted(d.pages)},
            )
        )

    assert_partition(source.name, source.pages, result)
    return result


def assert_partition(source_name: str, page_count: int, docs: list[LogicalDocument]) -> None:
    """Bất biến cứng: mỗi trang thuộc đúng MỘT logical document."""
    seen: dict[int, str] = {}
    for d in docs:
        for p in d.source_pages:
            if p in seen:
                raise PipelineError(
                    f"{source_name}: trang {p} bị gán cho 2 logical document "
                    f"({seen[p]} và {d.doc_key})."
                )
            seen[p] = d.doc_key
    missing = sorted(set(range(1, page_count + 1)) - set(seen))
    if missing:
        raise PipelineError(f"{source_name}: các trang chưa được gán tài liệu: {missing}")
