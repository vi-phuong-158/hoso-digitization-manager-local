"""Phase A+ — Incremental scan: đối chiếu inventory hiện tại với state registry.

Quyết định file nào Agent cần đọc (NEW/STALE) và file nào SKIP (dùng cache
hoặc đã PROCESSED). Module này CHỈ đọc SHA-256 (đã có sẵn từ pdf_inventory) và
tra registry — KHÔNG mở nội dung PDF, KHÔNG gọi provider. An toàn dùng cho
lệnh `status` (read-only, không mutate registry).

Khóa nhận diện là SHA-256, không phải filename:
  - cùng hash, khác tên/khác đường dẫn -> vẫn là cùng một nguồn, không đọc lại.
  - cùng tên, khác hash -> nguồn MỚI, phải xử lý.
  - nhiều file cùng hash trong một lần scan -> chỉ 1 bản canonical (tên nhỏ
    nhất theo alphabet để deterministic), các bản còn lại là DUPLICATE_SOURCE.

Hai khái niệm tách biệt (xem app/state.py):
  - AI đã đọc/phân tích xong (ANALYZED_PENDING_APPLY/REVIEW_REQUIRED) - nếu
    fingerprint (taxonomy/schema) còn khớp thì TÁI SỬ DỤNG, Agent không đọc lại.
  - Nghiệp vụ đã hoàn tất (PROCESSED) - chỉ khi mọi logical document đã được
    giải quyết + apply thành công + QC PASS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .fingerprint import Fingerprint
from .manifest import load_manifest
from .models import MODE_APPLY
from .pdf_inventory import PersonInventory, SourceFile
from .state import (
    STATUS_ANALYZED_PENDING_APPLY,
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_RETIRED,
    STATUS_PROCESSING,
    STATUS_REVIEW_REQUIRED,
    SourceState,
    StateRegistry,
)

DECISION_NEW = "NEW"
DECISION_STALE_ANALYSIS = "STALE_ANALYSIS"
DECISION_CACHED_PENDING_APPLY = "CACHED_PENDING_APPLY"
DECISION_CACHED_REVIEW_REQUIRED = "CACHED_REVIEW_REQUIRED"
DECISION_ALREADY_PROCESSED = "ALREADY_PROCESSED"
DECISION_FAILED_PREVIOUSLY = "FAILED_PREVIOUSLY"
DECISION_INTERRUPTED = "INTERRUPTED"
DECISION_DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
DECISION_RETIRED_SOURCE = "RETIRED_SOURCE"

ALL_DECISIONS = (
    DECISION_NEW,
    DECISION_STALE_ANALYSIS,
    DECISION_CACHED_PENDING_APPLY,
    DECISION_CACHED_REVIEW_REQUIRED,
    DECISION_ALREADY_PROCESSED,
    DECISION_FAILED_PREVIOUSLY,
    DECISION_INTERRUPTED,
    DECISION_DUPLICATE_SOURCE,
    DECISION_RETIRED_SOURCE,
)

# Quyết định coi là "đã có cache phân tích hợp lệ, Agent KHÔNG cần đọc lại".
CACHED_DECISIONS = (DECISION_CACHED_PENDING_APPLY, DECISION_CACHED_REVIEW_REQUIRED)


@dataclass
class SourceDecision:
    source: SourceFile
    decision: str
    record: Optional[SourceState] = None
    duplicate_of_hash: Optional[str] = None
    duplicate_of_name: Optional[str] = None
    needs_agent: bool = False  # Agent phải đọc PDF (NEW/STALE/retry)
    needs_apply: bool = False  # cần chạy naming+write khi apply (mọi ca trừ SKIP hẳn)
    output_mismatch: bool = False
    output_mismatch_detail: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "source_file": self.source.name,
            "decision": self.decision,
            "needs_agent": self.needs_agent,
            "needs_apply": self.needs_apply,
            "output_mismatch": self.output_mismatch,
            "output_mismatch_detail": self.output_mismatch_detail,
            "duplicate_of": self.duplicate_of_name,
        }


@dataclass
class IncrementalScan:
    person_folder: str
    decisions: list[SourceDecision] = field(default_factory=list)

    @property
    def needs_agent_sources(self) -> list[SourceFile]:
        return [d.source for d in self.decisions if d.needs_agent]

    @property
    def needs_apply_sources(self) -> list[SourceFile]:
        return [d.source for d in self.decisions if d.needs_apply]

    # Tương thích ngược với tên cũ (được vài chỗ/test tham chiếu).
    @property
    def to_process(self) -> list[SourceFile]:
        return self.needs_agent_sources

    def counts(self) -> dict[str, int]:
        out = {d: 0 for d in ALL_DECISIONS}
        for d in self.decisions:
            out[d.decision] += 1
        return out

    def as_dict(self) -> dict:
        return {
            "person_folder": self.person_folder,
            "total_pdf": len(self.decisions),
            "counts": self.counts(),
            "sources": [d.as_dict() for d in self.decisions],
        }

    def summary_text(self) -> str:
        c = self.counts()
        lines = [
            f"HỒ SƠ: {self.person_folder}",
            "",
            f"PDF hiện có: {len(self.decisions)}",
            "",
            f"Đã hoàn tất trước: {c[DECISION_ALREADY_PROCESSED]} -> SKIP",
            f"Mới bổ sung: {c[DECISION_NEW]}",
        ]
        if c[DECISION_STALE_ANALYSIS]:
            lines.append(f"Cache hết hạn (taxonomy/schema đổi): {c[DECISION_STALE_ANALYSIS]}")
        if c[DECISION_CACHED_PENDING_APPLY]:
            lines.append(f"Đã phân tích, chờ apply: {c[DECISION_CACHED_PENDING_APPLY]}")
        if c[DECISION_CACHED_REVIEW_REQUIRED]:
            lines.append(f"Đang chờ review: {c[DECISION_CACHED_REVIEW_REQUIRED]}")
        if c[DECISION_FAILED_PREVIOUSLY]:
            lines.append(f"Lỗi cũ: {c[DECISION_FAILED_PREVIOUSLY]}")
        if c[DECISION_INTERRUPTED]:
            lines.append(f"Bị gián đoạn lần trước (interrupted): {c[DECISION_INTERRUPTED]}")
        if c[DECISION_DUPLICATE_SOURCE]:
            lines.append(f"Trùng nội dung (duplicate): {c[DECISION_DUPLICATE_SOURCE]}")
        if c[DECISION_RETIRED_SOURCE]:
            lines.append(
                f"Source đã retire, chỉ giữ lịch sử: {c[DECISION_RETIRED_SOURCE]} -> SKIP"
            )
        mismatches = [d for d in self.decisions if d.output_mismatch]
        if mismatches:
            lines.append("")
            lines.append("CẢNH BÁO STATE_OUTPUT_MISMATCH:")
            for d in mismatches:
                lines.append(f"  - {d.source.name}: {d.output_mismatch_detail}")
        return "\n".join(lines)


def _check_output_mismatch(
    record: SourceState, source: SourceFile, output_dir: Path, review_dir: Path
) -> tuple[bool, Optional[str]]:
    if not record.manifest_path:
        return False, None
    manifest = load_manifest(Path(record.manifest_path))
    if manifest is None:
        return True, f"không đọc/parse được ledger đã ghi nhận: {record.manifest_path}"
    missing = []
    for entry in manifest.get("documents", []):
        if entry.get("source_file") != source.name:
            continue
        base = output_dir if entry.get("target_dir") == "output" else review_dir
        target = base / str(entry.get("target_file"))
        if not target.is_file():
            missing.append(entry.get("target_file"))
    if missing:
        return True, f"thiếu {len(missing)} file đầu ra đã ghi nhận trước đó: {missing}"
    return False, None


def scan_person_folder(
    inventory: PersonInventory,
    registry: StateRegistry,
    *,
    mode: str,
    fingerprint: Fingerprint,
    retry_review: bool = False,
    retry_failed: bool = False,
    output_dir: Optional[Path] = None,
    review_dir: Optional[Path] = None,
) -> IncrementalScan:
    by_hash: dict[str, list[SourceFile]] = {}
    for s in inventory.sources:
        by_hash.setdefault(s.sha256, []).append(s)

    decisions: list[SourceDecision] = []
    for h, files in by_hash.items():
        files_sorted = sorted(files, key=lambda f: f.name.casefold())
        canonical = files_sorted[0]
        record = registry.get(h)

        for f in files_sorted:
            if f is not canonical:
                decisions.append(
                    SourceDecision(
                        source=f, decision=DECISION_DUPLICATE_SOURCE, record=record,
                        duplicate_of_hash=h, duplicate_of_name=canonical.name,
                    )
                )
                continue
            decisions.append(_decide_canonical(f, record, fingerprint, mode, retry_review, retry_failed, output_dir, review_dir))

    order = {id(s): i for i, s in enumerate(inventory.sources)}
    decisions.sort(key=lambda d: order[id(d.source)])
    return IncrementalScan(person_folder=inventory.person_folder, decisions=decisions)


def _decide_canonical(
    f: SourceFile,
    record: Optional[SourceState],
    fingerprint: Fingerprint,
    mode: str,
    retry_review: bool,
    retry_failed: bool,
    output_dir: Optional[Path],
    review_dir: Optional[Path],
) -> SourceDecision:
    if record is None:
        return SourceDecision(source=f, decision=DECISION_NEW, needs_agent=True, needs_apply=(mode == MODE_APPLY))

    if record.status == STATUS_PROCESSING:
        # Còn sót PROCESSING từ lượt trước -> tiến trình bị gián đoạn giữa chừng.
        retry = retry_failed
        return SourceDecision(
            source=f, decision=DECISION_INTERRUPTED, record=record,
            needs_agent=retry, needs_apply=(retry and mode == MODE_APPLY),
        )

    if record.status == STATUS_RETIRED:
        return SourceDecision(
            source=f, decision=DECISION_RETIRED_SOURCE, record=record,
            needs_agent=False, needs_apply=False,
        )

    if record.status == STATUS_FAILED:
        retry = retry_failed
        return SourceDecision(
            source=f, decision=DECISION_FAILED_PREVIOUSLY, record=record,
            needs_agent=retry, needs_apply=(retry and mode == MODE_APPLY),
        )

    if record.status == STATUS_PROCESSED:
        mismatch, detail = (False, None)
        if output_dir is not None and review_dir is not None:
            mismatch, detail = _check_output_mismatch(record, f, output_dir, review_dir)
        return SourceDecision(
            source=f, decision=DECISION_ALREADY_PROCESSED, record=record,
            output_mismatch=mismatch, output_mismatch_detail=detail,
        )

    if record.status in (STATUS_ANALYZED_PENDING_APPLY, STATUS_REVIEW_REQUIRED):
        stale = not fingerprint.matches_cache(
            record.taxonomy_version or "", record.analysis_schema_version or ""
        )
        if stale:
            return SourceDecision(
                source=f, decision=DECISION_STALE_ANALYSIS, record=record,
                needs_agent=True, needs_apply=(mode == MODE_APPLY),
            )
        decision = (
            DECISION_CACHED_REVIEW_REQUIRED
            if record.status == STATUS_REVIEW_REQUIRED
            else DECISION_CACHED_PENDING_APPLY
        )
        # Cache còn tươi -> Agent KHÔNG đọc lại mặc định, kể cả khi apply. Chỉ
        # đọc lại nếu người vận hành yêu cầu rõ (--retry-review) - vd muốn Agent
        # nhìn lại toàn bộ thay vì chỉ dựa vào phần người dùng đã resolve tay.
        needs_agent = decision == DECISION_CACHED_REVIEW_REQUIRED and retry_review
        # Apply LUÔN xử lý nguồn có cache (để ghi/hoàn tất phần đã resolve -
        # Phase E) bất kể REVIEW_REQUIRED hay PENDING_APPLY.
        needs_apply = mode == MODE_APPLY
        return SourceDecision(
            source=f, decision=decision, record=record, needs_agent=needs_agent, needs_apply=needs_apply,
        )

    raise AssertionError(f"status không xử lý được: {record.status}")  # pragma: no cover
