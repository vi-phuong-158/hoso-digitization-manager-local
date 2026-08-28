"""Phase A — Inventory.

Chỉ ĐỌC file nguồn. Không rename/move/delete/ghi đè bất cứ thứ gì trong input/.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from pypdf import PdfReader

from .models import PageGeometry, PipelineError

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SourceFile:
    path: Path
    name: str
    sha256: str
    pages: int
    size_bytes: int
    geometry: dict[int, PageGeometry] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "file": self.name,
            "sha256": self.sha256,
            "pages": self.pages,
            "size_bytes": self.size_bytes,
        }


@dataclass
class PersonInventory:
    person_folder: str
    folder: Path
    sources: list[SourceFile]

    @property
    def total_pages(self) -> int:
        return sum(s.pages for s in self.sources)

    def by_name(self, name: str) -> SourceFile:
        for s in self.sources:
            if s.name == name:
                return s
        raise PipelineError(f"Không có file nguồn '{name}' trong {self.folder}")

    def as_dict(self) -> dict:
        return {
            "person_folder": self.person_folder,
            "sources": [s.as_dict() for s in self.sources],
            "total_pages": self.total_pages,
        }


def _page_geometry(reader: PdfReader) -> dict[int, PageGeometry]:
    geo: dict[int, PageGeometry] = {}
    for idx, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        try:
            rotation = int(page.get("/Rotate", 0) or 0) % 360
        except Exception:  # pragma: no cover - PDF lỗi metadata
            rotation = 0
        width = float(box.width)
        height = float(box.height)
        if rotation in (90, 270):
            width, height = height, width
        geo[idx] = PageGeometry(width=width, height=height, rotation=rotation)
    return geo


def read_source(path: Path) -> SourceFile:
    if not path.is_file():
        raise PipelineError(f"Không tìm thấy PDF: {path}")
    try:
        reader = PdfReader(str(path))
        n_pages = len(reader.pages)
        geometry = _page_geometry(reader)
    except Exception as exc:
        raise PipelineError(f"Không đọc được PDF '{path.name}': {exc}") from exc
    if n_pages == 0:
        raise PipelineError(f"PDF rỗng: {path.name}")
    return SourceFile(
        path=path,
        name=path.name,
        sha256=sha256_file(path),
        pages=n_pages,
        size_bytes=path.stat().st_size,
        geometry=geometry,
    )


def list_pdfs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise PipelineError(f"Không tìm thấy thư mục hồ sơ: {folder}")
    pdfs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
    # Thứ tự ổn định để dry-run idempotent; không phụ thuộc thứ tự filesystem.
    return sorted(pdfs, key=lambda p: p.name.casefold())


def build_inventory(folder: Path) -> PersonInventory:
    folder = Path(folder)
    pdfs = list_pdfs(folder)
    if not pdfs:
        raise PipelineError(f"Thư mục '{folder}' không có file PDF nào.")
    return PersonInventory(
        person_folder=folder.name,
        folder=folder,
        sources=[read_source(p) for p in pdfs],
    )


def verify_unchanged(inventory: PersonInventory) -> list[str]:
    """Trả về danh sách vi phạm nếu file nguồn bị đổi so với lúc inventory."""
    problems: list[str] = []
    for src in inventory.sources:
        if not src.path.is_file():
            problems.append(f"File nguồn biến mất: {src.name}")
            continue
        if sha256_file(src.path) != src.sha256:
            problems.append(f"SHA-256 file nguồn thay đổi: {src.name}")
    return problems


def geometry_similarity(a: Optional[PageGeometry], b: Optional[PageGeometry]) -> float:
    """0..1 — hai trang có cùng khổ giấy/kiểu scan hay không.

    Dùng làm bằng chứng ghép bìa/mặt sau (AGENTS.md mục 7: "màu/mẫu/chủ đề khớp").
    Hoàn toàn deterministic, không cần model.
    """
    if a is None or b is None:
        return 0.0
    dw = abs(a.width - b.width) / max(a.width, b.width, 1.0)
    dh = abs(a.height - b.height) / max(a.height, b.height, 1.0)
    diff = (dw + dh) / 2.0
    # 0% lệch -> 1.0 ; >= 5% lệch -> 0.0
    return max(0.0, 1.0 - diff / 0.05)


def iter_page_numbers(src: SourceFile) -> Iterable[int]:
    return range(1, src.pages + 1)
