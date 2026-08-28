"""Provider RUNTIME chính thức: đọc kết quả của Antigravity Runtime Agent.

Không API key. Không gọi mạng. Không SDK model. Provider này chỉ đọc file JSON
mà Agent đã ghi vào `analysis/<person_folder>/<pdf_stem>.json`, validate theo
`app/agent_contract.py`, rồi trả về cho pipeline deterministic.

Toàn bộ phần "nhận thức" (đọc PDF, nhìn trang, đọc ngày) do Agent làm bên trong
Antigravity. Toàn bộ phần "quyết định" (segment, ngưỡng, đặt tên, tách file, QC)
do code local làm.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from ..agent_contract import (
    ANALYSIS_DIRNAME,
    AgentAnalysis,
    AnalysisContractError,
    load_analysis,
    to_classification,
)
from ..catalog import Catalog, find_catalog_path, load_catalog
from ..models import UNKNOWN, DocumentClassification, PageObservation, TypeCandidate
from ..vision_adapter import DocumentVisionProvider, register_provider


class AgentAnalysisProvider(DocumentVisionProvider):
    name = "agent"

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        root = config.get("analysis_root")
        self.root = Path(root) if root else find_catalog_path().parent / ANALYSIS_DIRNAME
        self._catalog: Catalog = config.get("catalog") or load_catalog()
        self._cache: dict[str, AgentAnalysis] = {}

    # ---------------- nạp ----------------
    def analysis_path(self, pdf_path: Path) -> Path:
        pdf_path = Path(pdf_path)
        return self.root / pdf_path.parent.name / (pdf_path.stem + ".json")

    def _load(self, pdf_path: Path, expect_pages: Optional[int] = None) -> AgentAnalysis:
        pdf_path = Path(pdf_path)
        key = str(pdf_path.resolve())
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        analysis = load_analysis(
            self.analysis_path(pdf_path),
            self._catalog,
            expect_source=pdf_path.name,
            expect_pages=expect_pages,
        )
        self._cache[key] = analysis
        return analysis

    # ---------------- interface ----------------
    def analyze_pages(self, pdf_path: Path, page_numbers: Sequence[int]) -> list[PageObservation]:
        analysis = self._load(Path(pdf_path), expect_pages=len(page_numbers) or None)
        by_page = {o.page_number: o for o in analysis.pages}
        missing = [p for p in page_numbers if p not in by_page]
        if missing:
            raise AnalysisContractError(
                f"{analysis.raw_path}: Agent chưa mô tả các trang {missing}."
            )
        return [by_page[p] for p in page_numbers]

    def proposed_documents(self, pdf_path: Path) -> Optional[list[list[int]]]:
        return self._load(Path(pdf_path)).proposed_groups()

    def classify_document(
        self,
        pdf_path: Path,
        page_numbers: Sequence[int],
        candidates: Sequence[TypeCandidate],
        *,
        second_pass: bool = False,
        taxonomy: Optional[list[dict]] = None,
    ) -> DocumentClassification:
        analysis = self._load(Path(pdf_path))
        pages = list(page_numbers)
        entry = analysis.document_for(pages)
        if entry is not None:
            result = to_classification(entry)
            # Hợp đồng không có runner_up, nhưng hàng rào "cặp dễ nhầm" của
            # AGENTS.md mục 6 vẫn phải chạy -> suy runner_up từ ứng viên CẤP TRANG
            # mà chính Agent đã cung cấp.
            result.runner_up = self._runner_up_from_pages(analysis, pages, result.type_id)
            if second_pass:
                # Agent chỉ chạy một lượt trong Antigravity. Không có lượt 2 độc lập
                # -> không được tự nâng confidence; giữ nguyên và xin REVIEW.
                result.provider_note = "antigravity-agent:no-independent-second-pass"
                result.provider_needs_review = True
                result.provider_review_reason = (
                    result.provider_review_reason or "Không có lượt đọc lại độc lập"
                )
            return result

        # Segmenter local gom khác với đề xuất của Agent -> không có kết luận nào
        # cho đúng nhóm trang này. Suy ra từ tín hiệu cấp trang và xin REVIEW.
        return self._fallback_from_pages(analysis, pages)

    def _page_scores(self, analysis: AgentAnalysis, pages: list[int]) -> dict[str, float]:
        by_page = {o.page_number: o for o in analysis.pages}
        scores: dict[str, float] = {}
        for p in pages:
            o = by_page.get(p)
            if o is None:
                continue
            weight = 0.5 if o.is_attachment else 1.0
            for c in o.type_candidates:
                scores[c.type_id] = max(scores.get(c.type_id, 0.0), c.confidence * weight)
        return scores

    def _runner_up_from_pages(
        self, analysis: AgentAnalysis, pages: list[int], chosen: str
    ) -> Optional[TypeCandidate]:
        scores = {k: v for k, v in self._page_scores(analysis, pages).items() if k != chosen}
        if not scores:
            return None
        tid, conf = max(scores.items(), key=lambda kv: (kv[1], kv[0]))
        return TypeCandidate(tid, conf)

    def _fallback_from_pages(self, analysis: AgentAnalysis, pages: list[int]) -> DocumentClassification:
        by_page = {o.page_number: o for o in analysis.pages}
        obs_list = [by_page[p] for p in pages if p in by_page]
        scores: dict[str, float] = {}
        for o in obs_list:
            weight = 0.5 if o.is_attachment else 1.0
            for c in o.type_candidates:
                scores[c.type_id] = max(scores.get(c.type_id, 0.0), c.confidence * weight)
        if not scores:
            scores[UNKNOWN] = 1.0
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        content = [o for o in obs_list if not o.is_attachment] or obs_list
        date = next((o.document_date for o in obs_list if o.document_date), None)
        date_conf = next((o.date_confidence for o in obs_list if o.document_date), 0.0)
        return DocumentClassification(
            type_id=ranked[0][0],
            confidence=ranked[0][1],
            document_date=date,
            date_confidence=date_conf,
            title_short=next((o.title_guess for o in content if o.title_guess), None),
            runner_up=TypeCandidate(*ranked[1]) if len(ranked) > 1 else None,
            provider_note="antigravity-agent:page-fallback",
            provider_needs_review=True,
            provider_review_reason="Agent gom trang khác với segmenter local",
        )

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "runtime": "antigravity-native",
            "network": "none",
            "api_key_required": False,
            "analysis_root": str(self.root),
        }


register_provider("agent", lambda config: AgentAnalysisProvider(config))
