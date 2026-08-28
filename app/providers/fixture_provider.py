"""Provider fixture - thay thế model thật khi chạy test/golden acceptance.

Fixture CHỈ chứa tín hiệu cấp TRANG (những gì một model vision nhìn thấy trên
đúng một trang, cộng gợi ý hướng ghép). Fixture KHÔNG chứa:
  - cách gom trang thành tài liệu,
  - tên file đầu ra,
  - số thứ tự .1/.2,
  - trạng thái AUTO/REVIEW.

Nhờ vậy golden acceptance vẫn kiểm thử thật segmenter / classifier policy /
naming engine / QC, chứ không phải phát lại đáp án.

`classify_document` được suy ra một cách deterministic từ chính các observation
cấp trang của tài liệu đó, đúng như một classifier đọc cả logical document.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from ..models import DocumentClassification, PageObservation, TypeCandidate, UNKNOWN
from ..vision_adapter import (
    DocumentVisionProvider,
    ProviderError,
    register_provider,
)

DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "vision"

# Trang phụ (bìa/mặt sau) vẫn nói lên loại tài liệu nhưng yếu hơn trang nội dung.
ATTACHMENT_WEIGHT = 0.5


class FixtureVisionProvider(DocumentVisionProvider):
    name = "fixture"

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        root = config.get("fixture_root")
        self.root = Path(root) if root else DEFAULT_FIXTURE_ROOT
        self._cache: dict[str, dict[int, PageObservation]] = {}

    # ---------------- fixture loading ----------------
    def _fixture_path(self, pdf_path: Path) -> Path:
        return self.root / pdf_path.parent.name / (pdf_path.stem + ".json")

    def _load(self, pdf_path: Path) -> dict[int, PageObservation]:
        key = str(pdf_path.resolve())
        if key in self._cache:
            return self._cache[key]
        fpath = self._fixture_path(pdf_path)
        if not fpath.is_file():
            raise ProviderError(
                f"Không có fixture cho '{pdf_path.name}'. Mong đợi: {fpath}\n"
                f"Provider 'fixture' chỉ dùng cho hồ sơ đã có fixture; hồ sơ mới cần provider model thật."
            )
        raw = json.loads(fpath.read_text(encoding="utf-8"))
        pages: dict[int, PageObservation] = {}
        for item in raw.get("pages", []):
            obs = PageObservation(
                page_number=int(item["page_number"]),
                page_role=item.get("page_role", "CONTENT"),
                title_guess=item.get("title_guess"),
                document_date=item.get("document_date"),
                date_confidence=float(item.get("date_confidence", 0.0)),
                type_candidates=[
                    TypeCandidate(str(c["type_id"]), float(c["confidence"]))
                    for c in item.get("type_candidates", [])
                ],
                starts_new_document=bool(item.get("starts_new_document", False)),
                continues_previous=bool(item.get("continues_previous", False)),
                attach_hint=item.get("attach_hint", "NONE"),
                attach_hint_confidence=float(item.get("attach_hint_confidence", 0.0)),
                notes=item.get("notes"),
            )
            if obs.page_number in pages:
                raise ProviderError(f"Fixture {fpath.name}: trùng page_number {obs.page_number}")
            pages[obs.page_number] = obs
        if not pages:
            raise ProviderError(f"Fixture {fpath} không có trang nào.")
        self._cache[key] = pages
        return pages

    # ---------------- interface ----------------
    def analyze_pages(self, pdf_path: Path, page_numbers: Sequence[int]) -> list[PageObservation]:
        pages = self._load(Path(pdf_path))
        out: list[PageObservation] = []
        for p in page_numbers:
            if p not in pages:
                raise ProviderError(f"Fixture thiếu trang {p} của '{Path(pdf_path).name}'.")
            out.append(pages[p])
        return out

    def classify_document(
        self,
        pdf_path: Path,
        page_numbers: Sequence[int],
        candidates: Sequence[TypeCandidate],
        *,
        second_pass: bool = False,
        taxonomy: Optional[list[dict]] = None,
    ) -> DocumentClassification:
        pages = self._load(Path(pdf_path))
        obs_list = [pages[p] for p in sorted(page_numbers) if p in pages]
        if not obs_list:
            raise ProviderError(f"Fixture không có trang nào trong {list(page_numbers)}.")

        scores: dict[str, float] = {}
        for obs in obs_list:
            weight = ATTACHMENT_WEIGHT if obs.is_attachment else 1.0
            for cand in obs.type_candidates:
                scores[cand.type_id] = max(scores.get(cand.type_id, 0.0), cand.confidence * weight)
        if not scores:
            scores[UNKNOWN] = 1.0

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        top_id, top_conf = ranked[0]
        runner = TypeCandidate(ranked[1][0], ranked[1][1]) if len(ranked) > 1 else None

        content = [o for o in obs_list if not o.is_attachment] or obs_list
        date, date_conf = None, 0.0
        for o in obs_list:
            if o.document_date:
                date, date_conf = o.document_date, o.date_confidence
                break
        title = next((o.title_guess for o in content if o.title_guess), None)
        if title is None:
            title = next((o.title_guess for o in obs_list if o.title_guess), None)

        return DocumentClassification(
            type_id=top_id,
            confidence=round(top_conf, 4),
            document_date=date,
            date_confidence=date_conf,
            title_short=title,
            runner_up=runner,
            provider_note="fixture:second_pass" if second_pass else "fixture:first_pass",
        )

    def describe(self) -> dict:
        return {"provider": self.name, "fixture_root": str(self.root)}


register_provider("fixture", lambda config: FixtureVisionProvider(config))
