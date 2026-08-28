"""Phase L — State/output reconciliation.

Phát hiện lệch giữa state DB (`logical_documents.current_target_filename`) và
filesystem thật (`output/`, `review/`). CHỈ báo cáo — không tự xóa orphan,
không tự rebuild state nếu chưa đủ bằng chứng (đó là việc của
`app/state_import.py`, luôn cần bằng chứng ledger tường minh).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .pdf_inventory import list_pdfs, sha256_file
from .state import SOURCE_MISSING, SOURCE_RETIRED, StateRegistry


@dataclass
class ReconcileReport:
    person_folder: str
    missing_on_disk: list[str] = field(default_factory=list)  # DB nói có, đĩa không có
    orphans: list[str] = field(default_factory=list)  # đĩa có, DB không biết
    missing_sources: list[str] = field(default_factory=list)  # source bytes absent, not retired
    retired_sources: list[str] = field(default_factory=list)  # absence is expected

    @property
    def ok(self) -> bool:
        return not self.missing_on_disk and not self.orphans and not self.missing_sources

    def summary_text(self) -> str:
        lines = [f"RECONCILE: {self.person_folder}"]
        if self.ok:
            lines.append("  OK - state và filesystem khớp nhau.")
            for p in self.retired_sources:
                lines.append(f"  RETIRED (bytes vắng mặt được chấp nhận): {p}")
            return "\n".join(lines)
        for p in self.missing_on_disk:
            lines.append(f"  STATE_OUTPUT_MISMATCH (thiếu trên đĩa): {p}")
        for p in self.missing_sources:
            lines.append(f"  MISSING_SOURCE (chưa retire): {p}")
        for p in self.orphans:
            lines.append(f"  ORPHAN (có trên đĩa, state không biết): {p}")
        for p in self.retired_sources:
            lines.append(f"  RETIRED (bytes vắng mặt được chấp nhận): {p}")
        return "\n".join(lines)


def reconcile(
    registry: StateRegistry, person_folder: str, output_dir: Path, review_dir: Path,
    input_dir: Path | None = None,
) -> ReconcileReport:
    report = ReconcileReport(person_folder=person_folder)
    if input_dir is not None:
        present_hashes = {
            sha256_file(path) for path in list_pdfs(input_dir)
        } if Path(input_dir).is_dir() else set()
        for item in registry.source_lifecycle(person_folder, present_hashes):
            if item.lifecycle_status == SOURCE_MISSING:
                report.missing_sources.append(
                    f"{item.source_filename} (sha256 {item.source_hash})"
                )
            elif item.lifecycle_status == SOURCE_RETIRED:
                report.retired_sources.append(
                    f"{item.source_filename} (sha256 {item.source_hash})"
                )
    known_paths: set[Path] = set()
    for row in registry.logical_documents_for_person(person_folder):
        if not row.current_target_filename:
            continue
        base = output_dir if row.target_dir == "output" else review_dir
        path = base / row.current_target_filename
        known_paths.add(path.resolve())
        if not path.is_file():
            report.missing_on_disk.append(str(path))
    for base in (output_dir, review_dir):
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.pdf")):
            if f.resolve() not in known_paths:
                report.orphans.append(str(f))
    return report
