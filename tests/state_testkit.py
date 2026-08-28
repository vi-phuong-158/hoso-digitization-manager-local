"""Trợ giúp dựng dữ liệu tổng hợp cho test incremental/state.

Cố ý KHÔNG đụng tới `input/Vi Ngọc Phương/` (nhiệm vụ yêu cầu giữ hồ sơ đó cho
một bài kiểm thử "mù" riêng, không được đọc nội dung trong phiên DEV này).
Mọi PDF ở đây là PDF trắng tổng hợp; nội dung phân loại đến từ JSON fixture
tự viết, không cần model/Vision thật — vì các test này kiểm tra STATE MACHINE,
không kiểm tra chất lượng phân loại (đã có HAI_GOLDEN.json lo việc đó).
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Optional

from pypdf import PdfWriter


def derived_size(filename: str) -> tuple[float, float]:
    """Khổ trang suy ra ổn định từ tên file - đảm bảo hai filename KHÁC nhau
    tự động có nội dung (và SHA-256) khác nhau trừ khi test cố tình truyền
    `size=` giống nhau để dựng ca DUPLICATE_SOURCE có chủ đích."""
    offset = zlib.crc32(filename.encode("utf-8")) % 500
    return (420.0, 595.0 + offset / 10.0)


def make_pdf(path: Path, n_pages: int = 1, size: tuple[float, float] = (420.0, 595.0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=size[0], height=size[1])
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


def make_analysis(
    analysis_root: Path,
    person: str,
    pdf_name: str,
    *,
    page_count: int = 1,
    type_id: str = "04",
    confidence: float = 0.97,
    document_date: str | None = None,
    needs_review: bool = False,
    review_reason: str | None = None,
    title: str | None = None,
) -> Path:
    stem = Path(pdf_name).stem
    title = title or "Tài liệu tổng hợp"
    out = analysis_root / person / f"{stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    date_conf = 0.9 if document_date else 0.0
    payload = {
        "schema_version": "1.0",
        "produced_by": "test-synthetic",
        "person_folder": person,
        "source_file": pdf_name,
        "page_count": page_count,
        "pages": [
            {
                "page_number": p,
                "page_role": "CONTENT",
                "title_guess": f"Tài liệu tổng hợp trang {p}",
                "document_date": document_date,
                "date_confidence": date_conf,
                "type_candidates": [{"type_id": type_id, "confidence": confidence}],
                "starts_new_document": True,
                "continues_previous": False,
                "attach_hint": "NONE",
                "attach_hint_confidence": 0.0,
                "notes": None,
            }
            for p in range(1, page_count + 1)
        ],
        "documents": [
            {
                "source_pages": list(range(1, page_count + 1)),
                "type_id": type_id,
                "confidence": confidence,
                "document_date": document_date,
                "date_confidence": date_conf,
                "title_short": title,
                "needs_review": needs_review,
                "review_reason": review_reason,
            }
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return out


def add_source(
    input_root: Path,
    analysis_root: Path,
    person: str,
    filename: str,
    *,
    n_pages: int = 1,
    size: Optional[tuple[float, float]] = None,
    type_id: str = "04",
    confidence: float = 0.97,
    document_date: str | None = None,
    needs_review: bool = False,
    review_reason: str | None = None,
    title: str | None = None,
) -> Path:
    """Tạo một PDF tổng hợp + analysis JSON khớp nhau.

    Mặc định `size=None`: khổ trang suy ra ổn định từ TÊN FILE (`derived_size`),
    nên các nguồn khác tên tự động có hash khác nhau như dữ liệu thật. Muốn dựng
    ca DUPLICATE_SOURCE có chủ đích (2 tên khác nhau, cùng nội dung), truyền
    cùng một `size=` tường minh cho cả hai lần gọi.
    """
    pdf_path = input_root / person / filename
    make_pdf(pdf_path, n_pages=n_pages, size=size or derived_size(filename))
    make_analysis(
        analysis_root, person, filename, page_count=n_pages, type_id=type_id,
        confidence=confidence, document_date=document_date, needs_review=needs_review,
        review_reason=review_reason, title=title,
    )
    return pdf_path
