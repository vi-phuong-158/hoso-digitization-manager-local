"""Phase F - Write.

Mặc định dry-run. Chỉ `apply` mới tạo file.

Nguyên tắc bảo toàn hồ sơ:
- Tách PDF bằng PAGE OBJECT (PdfWriter.add_page) -> giữ nguyên nội dung/chất
  lượng trang, không rasterize, không nén lại.
- KHÔNG mở file nguồn ở chế độ ghi, không rename/move/delete file nguồn.
- Apply lặp lại phải idempotent: file đã đúng thì bỏ qua; file khác nội dung thì
  DỪNG (fail-safe), không âm thầm ghi đè và không tự tạo bản trùng.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from pypdf import PdfReader, PdfWriter

from .models import ClassifiedDocument, PipelineError
from .pdf_inventory import PersonInventory, sha256_file

LEDGER_FILENAME = "_manifest.json"
PRODUCER = "ho-so-dang-vien-pipeline"


def content_key(source_sha256: str, pages: Sequence[int]) -> str:
    """Khóa nội dung: cùng file nguồn + cùng dãy trang => cùng khóa."""
    raw = f"{source_sha256}|{','.join(str(p) for p in pages)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_pages(source_path: Path, pages: Sequence[int], target_path: Path) -> None:
    """Ghi các trang `pages` (1-based, giữ nguyên thứ tự) ra `target_path`."""
    reader = PdfReader(str(source_path))
    total = len(reader.pages)
    writer = PdfWriter()
    for p in pages:
        if not (1 <= p <= total):
            raise PipelineError(f"Trang {p} nằm ngoài '{source_path.name}' ({total} trang).")
        writer.add_page(reader.pages[p - 1])
    # Metadata cố định -> output deterministic, không nhúng thời điểm chạy.
    writer.add_metadata({"/Producer": PRODUCER, "/Creator": PRODUCER})
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_path.with_suffix(target_path.suffix + ".part")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    tmp.replace(target_path)


@dataclass
class WriteResult:
    written: list[str] = field(default_factory=list)
    skipped_identical: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    stale_in_output: list[str] = field(default_factory=list)
    output_sha256: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict:
        return {
            "written": self.written,
            "skipped_identical": self.skipped_identical,
            "conflicts": self.conflicts,
            "stale_files_left_untouched": self.stale_in_output,
        }


def _target_path(doc: ClassifiedDocument, output_dir: Path, review_dir: Path) -> Path:
    base = output_dir if doc.target_dir == "output" else review_dir
    return Path(base) / str(doc.target_file)


def plan_targets(
    documents: Sequence[ClassifiedDocument],
    inventory: PersonInventory,
    output_dir: Path,
    review_dir: Path,
) -> dict[str, dict]:
    """Bảng kế hoạch: đường dẫn đích -> {content_key, source, pages}. Dùng cho cả dry-run."""
    plan: dict[str, dict] = {}
    for doc in documents:
        src = inventory.by_name(doc.document.source_file)
        path = _target_path(doc, output_dir, review_dir)
        plan[str(path)] = {
            "target_file": doc.target_file,
            "target_dir": doc.target_dir,
            "source_file": src.name,
            "source_sha256": src.sha256,
            "source_pages": list(doc.document.source_pages),
            "content_key": content_key(src.sha256, doc.document.source_pages),
        }
    return plan


def apply_documents(
    documents: Sequence[ClassifiedDocument],
    inventory: PersonInventory,
    output_dir: Path,
    review_dir: Path,
    previous_ledger: Optional[dict] = None,
    force: bool = False,
) -> WriteResult:
    plan = plan_targets(documents, inventory, output_dir, review_dir)
    ledger = (previous_ledger or {}).get("targets") or {}
    result = WriteResult()

    # Vòng 1: phát hiện xung đột TRƯỚC khi ghi bất cứ thứ gì (fail-safe).
    to_write: list[tuple[Path, dict]] = []
    for path_str, entry in sorted(plan.items()):
        path = Path(path_str)
        if path.exists():
            known = ledger.get(entry["target_file"]) or {}
            actual_sha = sha256_file(path)
            same_source = known.get("content_key") == entry["content_key"]
            recorded_sha = known.get("output_sha256")
            # Khớp cả nguồn lẫn nội dung file đích -> đúng bản cũ, bỏ qua.
            if same_source and (recorded_sha is None or recorded_sha == actual_sha):
                result.skipped_identical.append(entry["target_file"])
                result.output_sha256[entry["target_file"]] = actual_sha
                continue
            if force:
                to_write.append((path, entry))
                continue
            why = (
                "nội dung file đích đã bị thay đổi so với lần apply trước"
                if same_source
                else "không khớp bản ghi trước"
            )
            result.conflicts.append(
                f"{entry['target_file']}: đã tồn tại nhưng {why} "
                f"(nguồn {entry['source_file']} trang {entry['source_pages']})."
            )
            continue
        to_write.append((path, entry))

    if result.conflicts:
        return result

    for path, entry in to_write:
        src = inventory.by_name(entry["source_file"])
        split_pages(src.path, entry["source_pages"], path)
        result.written.append(entry["target_file"])
        result.output_sha256[entry["target_file"]] = sha256_file(path)

    # Báo (không xóa) các file lạ còn sót trong output/review của người này.
    expected = {Path(p).name for p in plan}
    for d in (output_dir, review_dir):
        if not Path(d).is_dir():
            continue
        for f in sorted(Path(d).iterdir()):
            if f.is_file() and f.suffix.lower() == ".pdf" and f.name not in expected:
                result.stale_in_output.append(str(f))
    return result


def verify_outputs(
    documents: Sequence[ClassifiedDocument], output_dir: Path, review_dir: Path
) -> list[str]:
    """Kiểm tra file đầu ra mở được và đúng số trang."""
    problems: list[str] = []
    for doc in documents:
        path = _target_path(doc, output_dir, review_dir)
        if not path.is_file():
            problems.append(f"Thiếu file đầu ra: {path}")
            continue
        try:
            n = len(PdfReader(str(path)).pages)
        except Exception as exc:
            problems.append(f"Không mở được file đầu ra '{path.name}': {exc}")
            continue
        if n != len(doc.document.source_pages):
            problems.append(
                f"'{path.name}' có {n} trang, kỳ vọng {len(doc.document.source_pages)}."
            )
    return problems


def output_hashes(paths: Sequence[Path]) -> dict[str, str]:
    return {p.name: sha256_file(p) for p in paths if p.is_file()}
