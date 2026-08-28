"""Phase G - Integrity / QC.

Đây là hàng rào cuối. Nếu bất kỳ check nào FAIL thì pipeline không được coi là
"ổn": dry-run báo BLOCKED_QC, apply bị chặn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .catalog import Catalog
from .models import UNKNOWN, ClassifiedDocument
from .naming import REVIEW_PREFIX
from .pdf_inventory import PersonInventory, verify_unchanged


@dataclass
class QCCheck:
    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"check": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class QCReport:
    checks: list[QCCheck] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(QCCheck(name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[QCCheck]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [c.as_dict() for c in self.checks],
            # Các khóa tóm tắt theo AGENTS.md mục 8.
            "all_pages_accounted_for": self._flag("page_coverage"),
            "no_page_overlap": self._flag("page_overlap"),
            "filename_collision": not self._flag("filename_collision"),
        }

    def _flag(self, name: str) -> bool:
        for c in self.checks:
            if c.name == name:
                return c.passed
        return False


def run_qc(
    catalog: Catalog,
    inventory: PersonInventory,
    documents: Sequence[ClassifiedDocument],
    output_dir: Path,
    review_dir: Path,
    output_problems: Optional[list[str]] = None,
    sources: Optional[Sequence] = None,
) -> QCReport:
    """`sources`: phạm vi cần kiểm coverage/overlap (mặc định = toàn bộ inventory).

    Khi chạy incremental, chỉ các nguồn được xử lý THẬT trong lượt này mới cần
    kiểm coverage; các nguồn đã SKIP (ALREADY_PROCESSED/DUPLICATE_SOURCE/...)
    đã được QC ở lượt xử lý trước đó (hoặc không bao giờ được xử lý), không có
    `documents` nào trong lượt này nên không được tính là "thiếu trang".
    """
    report = QCReport()
    scope = list(sources) if sources is not None else list(inventory.sources)
    scope_names = {s.name for s in scope}
    total_pages = sum(s.pages for s in scope)

    # 1) Page coverage & overlap, theo từng file nguồn trong phạm vi.
    coverage_problems: list[str] = []
    overlap_problems: list[str] = []
    for src in scope:
        seen: dict[int, str] = {}
        for doc in documents:
            if doc.document.source_file != src.name:
                continue
            for p in doc.document.source_pages:
                if p in seen:
                    overlap_problems.append(f"{src.name} trang {p}: {seen[p]} & {doc.document.doc_key}")
                seen[p] = doc.document.doc_key
        missing = sorted(set(range(1, src.pages + 1)) - set(seen))
        if missing:
            coverage_problems.append(f"{src.name}: thiếu trang {missing}")
        extra = sorted(p for p in seen if p < 1 or p > src.pages)
        if extra:
            coverage_problems.append(f"{src.name}: trang ngoài phạm vi {extra}")

    covered = sum(
        len(d.document.source_pages) for d in documents if d.document.source_file in scope_names
    )
    report.add(
        "page_coverage",
        not coverage_problems and covered == total_pages,
        f"{covered}/{total_pages} trang được gán tài liệu. " + "; ".join(coverage_problems),
    )
    report.add("page_overlap", not overlap_problems, "; ".join(overlap_problems) or "0 trang bị dùng lặp")

    # 2) Nguồn không bị thay đổi.
    mutations = verify_unchanged(inventory)
    report.add("source_unchanged", not mutations, "; ".join(mutations) or "SHA-256 file nguồn giữ nguyên")

    # 3) Mọi logical document phải có trạng thái AUTO hoặc REVIEW.
    bad_status = [
        d.document.doc_key for d in documents if d.final_status not in ("AUTO", "REVIEW")
    ]
    report.add(
        "status_coverage",
        not bad_status and len(documents) > 0,
        "; ".join(bad_status) or f"{len(documents)} logical document đều có trạng thái",
    )

    # 4) Tên file AUTO phải sinh từ catalog.
    naming_problems: list[str] = []
    valid_bases = {t.filename_base for t in catalog.all_types()}
    for d in documents:
        name = d.target_file or ""
        if d.final_status == "AUTO":
            if d.classification.type_id == UNKNOWN:
                naming_problems.append(f"{d.document.doc_key}: AUTO nhưng type UNKNOWN")
                continue
            stem = name[:-4] if name.endswith(".pdf") else name
            base, _, seq = stem.rpartition(".")
            if base in valid_bases and seq.isdigit():
                continue
            if stem in valid_bases:
                continue
            naming_problems.append(f"{d.document.doc_key}: tên '{name}' không sinh từ catalog")
        else:
            if not name.startswith(REVIEW_PREFIX):
                naming_problems.append(f"{d.document.doc_key}: file REVIEW phải có tiền tố {REVIEW_PREFIX}")
    report.add(
        "naming_from_catalog",
        not naming_problems,
        "; ".join(naming_problems) or "100% tên file AUTO lấy từ document_types.json",
    )

    # 5) Không trùng tên file đích.
    targets: dict[str, str] = {}
    collisions: list[str] = []
    for d in documents:
        base = output_dir if d.target_dir == "output" else review_dir
        key = str(Path(base) / str(d.target_file))
        if key in targets:
            collisions.append(f"{key}: {targets[key]} & {d.document.doc_key}")
        targets[key] = d.document.doc_key
    report.add("filename_collision", not collisions, "; ".join(collisions) or "không trùng tên")

    # 6) Số lượng logical document hợp lý.
    sane = 1 <= len(documents) <= total_pages
    report.add(
        "document_count_sane",
        sane,
        f"{len(documents)} logical document / {total_pages} trang nguồn",
    )

    # 7) Kiểm tra file đầu ra (chỉ chạy sau apply).
    if output_problems is not None:
        report.add(
            "outputs_readable",
            not output_problems,
            "; ".join(output_problems) or "mọi file đầu ra mở được và đúng số trang",
        )

    return report
