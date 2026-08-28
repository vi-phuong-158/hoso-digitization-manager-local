"""State registry — theo dõi PDF nào đã PROCESSED để không đọc lại (Antigravity Agent).

Lưu trên SQLite local (`state/processing_state.db`). Đây KHÔNG phải database
server; chỉ là một file local, không thêm dịch vụ ngoài. Không lưu toàn văn
hồ sơ hay OCR — chỉ lưu metadata điều phối tối thiểu.

Khóa nhận diện DUY NHẤT là SHA-256 của toàn bộ file PDF nguồn, không phải
filename. Không được sửa/đánh dấu vào chính PDF nguồn.

Hai khái niệm tách biệt (đây là điểm khác biệt cốt lõi so với bản đầu tiên):
  - AI đã đọc/phân tích xong nguồn      -> ANALYZED_PENDING_APPLY / REVIEW_REQUIRED
  - Nghiệp vụ đã hoàn tất (mọi logical
    document đã được giải quyết + ghi
    file thật + QC PASS)                -> PROCESSED

Một nguồn có tài liệu REVIEW chưa được người vận hành chốt thì KHÔNG BAO GIỜ
tự chuyển thành PROCESSED, dù đã apply và đã copy ra `review/`. Một source đã
mất bytes được phát hiện là `MISSING_SOURCE`; chỉ operator mới có thể tạo
tombstone `RETIRED`, không xóa source identity hoặc logical-document history.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__ as PIPELINE_VERSION
from .models import PipelineError
from .policy import (
    CLASSIFICATION_KIND_DUPLICATE,
    CLASSIFICATION_KIND_TAXONOMY,
    CLASSIFICATION_KINDS,
    DATE_PRECISIONS,
    DATE_PRECISION_UNKNOWN,
    validate_classification_metadata,
)

DB_FILENAME = "processing_state.db"
STATE_SCHEMA_VERSION = 5

STATUS_PROCESSING = "PROCESSING"
STATUS_ANALYZED_PENDING_APPLY = "ANALYZED_PENDING_APPLY"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATUS_PROCESSED = "PROCESSED"
STATUS_FAILED = "FAILED"
STATUS_RETIRED = "RETIRED"

SOURCE_ACTIVE = "ACTIVE"
SOURCE_MISSING = "MISSING_SOURCE"
SOURCE_RETIRED = STATUS_RETIRED

# Các giá trị hợp lệ duy nhất cho cột `status` trong bảng sources.
PERSISTED_STATUSES = (
    STATUS_PROCESSING,
    STATUS_ANALYZED_PENDING_APPLY,
    STATUS_REVIEW_REQUIRED,
    STATUS_PROCESSED,
    STATUS_FAILED,
    STATUS_RETIRED,
)

RESOLUTION_AUTO_RESOLVED = "AUTO_RESOLVED"
RESOLUTION_REVIEW_PENDING = "REVIEW_PENDING"
RESOLUTION_REVIEW_RESOLVED = "REVIEW_RESOLVED"

_MAX_ERROR_LEN = 2000  # chặn last_error phình to thành nơi chép nội dung tài liệu
_MAX_TITLE_LEN = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def logical_document_id(source_hash: str, source_pages: list[int]) -> str:
    """ID ổn định, không đổi qua các lần renumber (Phase K).

    Chỉ phụ thuộc (source_hash, source_pages) — hai thứ KHÔNG đổi theo thời
    gian cho cùng một logical document, dù filename/sequence có bị tính lại.
    """
    import hashlib

    raw = source_hash + "|" + ",".join(str(p) for p in source_pages)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class SourceState:
    source_hash: str
    source_filename: str
    source_relative_path: str
    person_folder: str
    page_count: int
    status: str
    first_seen_at: str
    processing_started_at: Optional[str]
    processed_at: Optional[str]
    logical_document_count: Optional[int]
    manifest_path: Optional[str]
    last_error: Optional[str]
    pipeline_version: str
    taxonomy_version: Optional[str]
    analysis_schema_version: Optional[str]
    last_seen_path: str
    updated_at: str
    previous_status: Optional[str]
    retired_at: Optional[str]
    retired_reason: Optional[str]
    retired_by: Optional[str]
    audit_provenance: Optional[str]

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class LogicalDocumentRow:
    logical_document_id: str
    source_hash: str
    source_pages: list[int]
    type_id: str
    confidence: float
    document_date: Optional[str]
    date_confidence: float
    title_short: Optional[str]
    segmentation_flags: list[str]
    classification_status: str  # AUTO | REVIEW (trục phân loại, Phase D)
    classification_reasons: list[str]
    resolution_status: str  # AUTO_RESOLVED | REVIEW_PENDING | REVIEW_RESOLVED
    resolved_type_id: Optional[str]
    resolved_document_date: Optional[str]
    resolved_by: Optional[str]
    resolved_at: Optional[str]
    current_target_filename: Optional[str]
    target_dir: Optional[str]
    sequence_index: Optional[int]
    created_at: str
    updated_at: str
    # --- DEV POLICY CLOSURE: type 87 subtype / supporting / duplicate / partial date ---
    classification_kind: str  # TAXONOMY | SUPPORTING_DOCUMENT | DUPLICATE (lúc phân tích)
    subtype: Optional[str]  # metadata phụ khi type_id == "87" (không đổi filename)
    date_precision: Optional[str]  # DAY | MONTH | YEAR | UNKNOWN (lúc phân tích)
    duplicate_of: Optional[str]  # logical_document_id bị trùng, chỉ có khi kind=DUPLICATE
    resolved_classification_kind: Optional[str]
    resolved_subtype: Optional[str]
    resolved_date_precision: Optional[str]

    @property
    def effective_type_id(self) -> str:
        return self.resolved_type_id or self.type_id

    @property
    def effective_document_date(self) -> Optional[str]:
        return self.resolved_document_date or self.document_date

    @property
    def effective_date_precision(self) -> Optional[str]:
        return self.resolved_date_precision or self.date_precision or DATE_PRECISION_UNKNOWN

    @property
    def effective_classification_kind(self) -> str:
        return self.resolved_classification_kind or self.classification_kind

    @property
    def effective_subtype(self) -> Optional[str]:
        return self.resolved_subtype or self.subtype

    @property
    def is_settled(self) -> bool:
        """Đã có filename chính thức được không (không còn REVIEW_PENDING)."""
        return self.resolution_status != RESOLUTION_REVIEW_PENDING

    @property
    def is_nameable(self) -> bool:
        """DUPLICATE không bao giờ có output riêng - loại khỏi mọi naming pool."""
        return self.effective_classification_kind != CLASSIFICATION_KIND_DUPLICATE

    def as_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["source_pages"] = list(self.source_pages)
        d["segmentation_flags"] = list(self.segmentation_flags)
        d["classification_reasons"] = list(self.classification_reasons)
        d["date_precision"] = d["date_precision"] or DATE_PRECISION_UNKNOWN
        d["resolved_date_precision"] = d["resolved_date_precision"] or DATE_PRECISION_UNKNOWN
        return d


@dataclass(frozen=True)
class LegacyHydrationResult:
    """Kết quả một lần khôi phục logical state từ ledger legacy.

    Chỉ các ID trong ``restored_logical_document_ids`` mới vừa được INSERT.
    Một lượt hoàn toàn đã hydrate trả về danh sách rỗng và không ghi lại DB.
    """

    restored_logical_document_ids: tuple[str, ...]
    source_status: str


@dataclass(frozen=True)
class SourceLifecycle:
    source_hash: str
    source_filename: str
    page_count: int
    state_status: str
    lifecycle_status: str
    previous_status: Optional[str]
    retired_at: Optional[str]
    retired_reason: Optional[str]
    retired_by: Optional[str]
    audit_provenance: Optional[str]

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class SourceRetirementResult:
    outcome: str  # RETIRED | ALREADY_RETIRED
    source_hash: str
    source_filename: str
    status: str
    previous_status: str
    retired_at: Optional[str]
    retired_reason: Optional[str]
    retired_by: Optional[str]
    audit_provenance: Optional[str]

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _row_to_logical_document(row: sqlite3.Row) -> LogicalDocumentRow:
    d = dict(row)
    d["source_pages"] = json.loads(d["source_pages"])
    d["segmentation_flags"] = json.loads(d["segmentation_flags"])
    d["classification_reasons"] = json.loads(d["classification_reasons"])
    return LogicalDocumentRow(**d)


class StateRegistry:
    """Lớp truy cập SQLite. Mỗi phương thức mutate là MỘT transaction atomic."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        # WAL + synchronous=FULL: an toàn khi crash giữa chừng, vẫn đủ nhanh cho
        # một tool local một người dùng. Không cần cấu hình gì thêm phía người dùng.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._migrate_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateRegistry":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_hash TEXT PRIMARY KEY,
                    source_filename TEXT NOT NULL,
                    source_relative_path TEXT NOT NULL,
                    person_folder TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('PROCESSING','ANALYZED_PENDING_APPLY','REVIEW_REQUIRED','PROCESSED','FAILED')),
                    first_seen_at TEXT NOT NULL,
                    processing_started_at TEXT,
                    processed_at TEXT,
                    logical_document_count INTEGER,
                    manifest_path TEXT,
                    last_error TEXT,
                    pipeline_version TEXT NOT NULL,
                    taxonomy_version TEXT,
                    analysis_schema_version TEXT,
                    last_seen_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_person ON sources(person_folder)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_retirements (
                    source_hash TEXT PRIMARY KEY REFERENCES sources(source_hash),
                    status TEXT NOT NULL CHECK(status = 'RETIRED'),
                    previous_status TEXT NOT NULL CHECK(previous_status IN
                        ('PROCESSING','ANALYZED_PENDING_APPLY','REVIEW_REQUIRED','PROCESSED','FAILED')),
                    retired_at TEXT NOT NULL,
                    retired_reason TEXT,
                    retired_by TEXT NOT NULL,
                    audit_provenance TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logical_documents (
                    logical_document_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL REFERENCES sources(source_hash),
                    source_pages TEXT NOT NULL,
                    type_id TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    document_date TEXT,
                    date_confidence REAL NOT NULL,
                    title_short TEXT,
                    segmentation_flags TEXT NOT NULL,
                    classification_status TEXT NOT NULL CHECK(classification_status IN ('AUTO','REVIEW')),
                    classification_reasons TEXT NOT NULL,
                    resolution_status TEXT NOT NULL CHECK(resolution_status IN
                        ('AUTO_RESOLVED','REVIEW_PENDING','REVIEW_RESOLVED')),
                    resolved_type_id TEXT,
                    resolved_document_date TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    current_target_filename TEXT,
                    target_dir TEXT,
                    sequence_index INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    classification_kind TEXT NOT NULL DEFAULT 'TAXONOMY',
                    subtype TEXT,
                    date_precision TEXT,
                    duplicate_of TEXT,
                    resolved_classification_kind TEXT,
                    resolved_subtype TEXT,
                    resolved_date_precision TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logdocs_source ON logical_documents(source_hash)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS state_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            # Review/repair metadata lives in this same local state DB.  It
            # never stores PDF bytes or OCR/full-text payloads.
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS review_sessions (
                    session_id TEXT PRIMARY KEY, person_folder TEXT NOT NULL,
                    scope_json TEXT NOT NULL, base_revision INTEGER NOT NULL,
                    review_method TEXT NOT NULL, model_metadata_json TEXT NOT NULL,
                    finding_count INTEGER NOT NULL DEFAULT 0, review_status TEXT NOT NULL,
                    repair_status TEXT NOT NULL, result_revision INTEGER,
                    created_at TEXT NOT NULL, completed_at TEXT
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS review_findings (
                    finding_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES review_sessions(session_id),
                    source_hash TEXT, page_start INTEGER, page_end INTEGER,
                    finding_type TEXT NOT NULL, severity TEXT NOT NULL,
                    existing_result_json TEXT NOT NULL, proposed_result_json TEXT NOT NULL,
                    reason TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL,
                    decision TEXT, decision_payload_json TEXT, reviewed_by TEXT,
                    reviewed_at TEXT, evidence_json TEXT, fingerprint TEXT,
                    reviewer_version TEXT, created_at TEXT NOT NULL
                )
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_review_findings_session ON review_findings(session_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_review_findings_suppression ON review_findings(fingerprint, reviewer_version, decision)")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS case_revisions (
                    person_folder TEXT NOT NULL, revision INTEGER NOT NULL,
                    parent_revision INTEGER, kind TEXT NOT NULL, summary TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(person_folder, revision)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS repair_plans (
                    repair_plan_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES review_sessions(session_id),
                    person_folder TEXT NOT NULL, base_revision INTEGER NOT NULL,
                    plan_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    applied_at TEXT, result_revision INTEGER
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS correction_ledger (
                    correction_id TEXT PRIMARY KEY,
                    repair_plan_id TEXT NOT NULL REFERENCES repair_plans(repair_plan_id),
                    finding_id TEXT NOT NULL REFERENCES review_findings(finding_id),
                    person_folder TEXT NOT NULL, source_hash TEXT,
                    source_pages_json TEXT NOT NULL, finding_type TEXT NOT NULL,
                    old_result_json TEXT NOT NULL, new_result_json TEXT NOT NULL,
                    decision TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(repair_plan_id, finding_id)
                )
            """)

    # Cột thêm sau bản gốc (Phase 30 - DEV POLICY CLOSURE). ALTER TABLE ADD COLUMN
    # thay vì CREATE lại, để không đụng dữ liệu sẵn có (section 14: migration
    # backward compatible, không xoá state hiện có, transactional).
    _LOGICAL_DOC_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
        ("classification_kind", "TEXT NOT NULL DEFAULT 'TAXONOMY'"),
        ("subtype", "TEXT"),
        ("date_precision", "TEXT"),
        ("duplicate_of", "TEXT"),
        ("resolved_classification_kind", "TEXT"),
        ("resolved_subtype", "TEXT"),
        ("resolved_date_precision", "TEXT"),
    )
    _REVIEW_FINDING_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
        ("evidence_json", "TEXT"),
        ("fingerprint", "TEXT"),
        ("reviewer_version", "TEXT"),
    )

    def _migrate_schema(self) -> None:
        existing = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(logical_documents)").fetchall()
        }
        # Explicit BEGIN is important here: Python's sqlite3 context manager
        # does not start a transaction for DDL-only work, so an ALTER could
        # otherwise be committed before a later migration step fails.
        self._conn.execute("BEGIN")
        try:
            for name, decl in self._LOGICAL_DOC_NEW_COLUMNS:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE logical_documents ADD COLUMN {name} {decl}")
            review_columns = {
                r["name"] for r in self._conn.execute("PRAGMA table_info(review_findings)").fetchall()
            }
            for name, decl in self._REVIEW_FINDING_NEW_COLUMNS:
                if name not in review_columns:
                    self._conn.execute(f"ALTER TABLE review_findings ADD COLUMN {name} {decl}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_review_findings_suppression ON review_findings(fingerprint, reviewer_version, decision)")
            self._conn.execute(
                "INSERT INTO state_schema(key, value) VALUES ('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(STATE_SCHEMA_VERSION),),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @property
    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM state_schema WHERE key = 'version'"
        ).fetchone()
        return int(row[0]) if row else 1

    # ================= sources: truy vấn (read-only) =================
    _SOURCE_SELECT = """
        SELECT
            s.source_hash, s.source_filename, s.source_relative_path, s.person_folder,
            s.page_count, COALESCE(r.status, s.status) AS status,
            s.first_seen_at, s.processing_started_at, s.processed_at,
            s.logical_document_count, s.manifest_path, s.last_error,
            s.pipeline_version, s.taxonomy_version, s.analysis_schema_version,
            s.last_seen_path, s.updated_at,
            r.previous_status, r.retired_at, r.retired_reason, r.retired_by,
            r.audit_provenance
        FROM sources s
        LEFT JOIN source_retirements r ON r.source_hash = s.source_hash
    """

    def get(self, source_hash: str) -> Optional[SourceState]:
        row = self._conn.execute(
            self._SOURCE_SELECT + " WHERE s.source_hash = ?", (source_hash,)
        ).fetchone()
        return SourceState(**dict(row)) if row else None

    def all(self, person_folder: Optional[str] = None) -> list[SourceState]:
        query = self._SOURCE_SELECT
        params: tuple = ()
        if person_folder is not None:
            query += " WHERE s.person_folder = ?"
            params = (person_folder,)
        query += " ORDER BY s.source_hash"
        rows = self._conn.execute(query, params).fetchall()
        return [SourceState(**dict(r)) for r in rows]

    def source_lifecycle(
        self, person_folder: str, present_hashes: Optional[set[str]] = None
    ) -> list[SourceLifecycle]:
        """Classify known sources without changing their persisted history.

        ``present_hashes`` comes from the current physical inventory.  When it
        is omitted, every non-retired source is treated as present, which keeps
        state-only callers deterministic while CLI/reporting passes inventory
        hashes for real MISSING_SOURCE detection.
        """
        present = present_hashes
        out: list[SourceLifecycle] = []
        for source in self.all(person_folder):
            if source.status == STATUS_RETIRED:
                lifecycle = SOURCE_RETIRED
            elif present is None or source.source_hash in present:
                lifecycle = SOURCE_ACTIVE
            else:
                lifecycle = SOURCE_MISSING
            out.append(
                SourceLifecycle(
                    source_hash=source.source_hash,
                    source_filename=source.source_filename,
                    page_count=source.page_count,
                    state_status=source.status,
                    lifecycle_status=lifecycle,
                    previous_status=source.previous_status,
                    retired_at=source.retired_at,
                    retired_reason=source.retired_reason,
                    retired_by=source.retired_by,
                    audit_provenance=source.audit_provenance,
                )
            )
        return out

    def retire_source(
        self,
        source_hash: str,
        *,
        physical_hashes: set[str],
        reason: Optional[str] = None,
        retired_by: str = "operator",
        audit_provenance: str = "cli:retire-source",
    ) -> SourceRetirementResult:
        """Explicitly tombstone one absent source, preserving its identity.

        The caller supplies the current physical inventory hashes.  This
        makes the safety check part of the state transition API: a source that
        is present, renamed, or copied under another filename cannot be
        retired accidentally.  Repeating the same command is a read-only
        ``ALREADY_RETIRED`` result and does not create a second audit event.
        """
        source = self.get(source_hash)
        if source is None:
            raise PipelineError(f"retire-source: source hash không tồn tại trong state: {source_hash}")
        if source.source_hash in physical_hashes:
            raise PipelineError(
                f"retire-source: từ chối vì source vẫn tồn tại trên đĩa: {source.source_filename}"
            )
        if source.status == STATUS_RETIRED:
            return SourceRetirementResult(
                outcome="ALREADY_RETIRED",
                source_hash=source.source_hash,
                source_filename=source.source_filename,
                status=source.status,
                previous_status=source.previous_status or STATUS_PROCESSED,
                retired_at=source.retired_at,
                retired_reason=source.retired_reason,
                retired_by=source.retired_by,
                audit_provenance=source.audit_provenance,
            )
        if not retired_by or len(retired_by) > 200:
            raise PipelineError("retire-source: retired_by phải là chuỗi 1..200 ký tự.")
        if reason is not None and len(reason) > _MAX_ERROR_LEN:
            raise PipelineError(f"retire-source: reason quá dài (>{_MAX_ERROR_LEN} ký tự).")
        if not audit_provenance or len(audit_provenance) > 200:
            raise PipelineError("retire-source: audit_provenance không hợp lệ.")

        previous_status = source.status
        ts = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO source_retirements(
                    source_hash, status, previous_status, retired_at,
                    retired_reason, retired_by, audit_provenance
                ) VALUES (?, 'RETIRED', ?, ?, ?, ?, ?)
                """,
                (source_hash, previous_status, ts, reason, retired_by, audit_provenance),
            )
        retired = self.get(source_hash)
        assert retired is not None
        return SourceRetirementResult(
            outcome="RETIRED",
            source_hash=retired.source_hash,
            source_filename=retired.source_filename,
            status=retired.status,
            previous_status=retired.previous_status or previous_status,
            retired_at=retired.retired_at,
            retired_reason=retired.retired_reason,
            retired_by=retired.retired_by,
            audit_provenance=retired.audit_provenance,
        )

    def export_json(self) -> dict:
        return {
            "schema": "processing_state.v3",
            "exported_at": _now(),
            "sources": [s.as_dict() for s in self.all()],
            "logical_documents": [d.as_dict() for d in self._all_logical_documents()],
        }

    def _all_logical_documents(self) -> list[LogicalDocumentRow]:
        rows = self._conn.execute(
            "SELECT * FROM logical_documents ORDER BY logical_document_id"
        ).fetchall()
        return [_row_to_logical_document(r) for r in rows]

    # ================= sources: mutate =================
    def begin_processing(
        self,
        *,
        source_hash: str,
        source_filename: str,
        source_relative_path: str,
        person_folder: str,
        page_count: int,
    ) -> None:
        """NEW -> PROCESSING, hoặc retry (FAILED/REVIEW_REQUIRED/STALE/INTERRUPTED) -> PROCESSING.

        Phải được gọi và COMMIT xong TRƯỚC KHI đưa PDF cho Agent đọc, để nếu
        tiến trình bị crash giữa chừng, lần chạy sau phát hiện được PROCESSING
        còn sót lại (INTERRUPTED) thay vì mất dấu. Xoá sạch logical_documents
        cũ (nếu có) vì phân tích sắp được làm lại từ đầu.
        """
        existing = self.get(source_hash)
        if existing is not None and existing.status == STATUS_RETIRED:
            raise PipelineError(
                f"Không thể xử lý lại source RETIRED {source_hash[:12]}…; cần policy operator khác."
            )
        ts = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sources (
                    source_hash, source_filename, source_relative_path, person_folder,
                    page_count, status, first_seen_at, processing_started_at,
                    processed_at, logical_document_count, manifest_path, last_error,
                    pipeline_version, taxonomy_version, analysis_schema_version,
                    last_seen_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PROCESSING', ?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL, ?, ?)
                ON CONFLICT(source_hash) DO UPDATE SET
                    status = 'PROCESSING',
                    processing_started_at = excluded.processing_started_at,
                    last_error = NULL,
                    pipeline_version = excluded.pipeline_version,
                    taxonomy_version = NULL,
                    analysis_schema_version = NULL,
                    source_filename = excluded.source_filename,
                    source_relative_path = excluded.source_relative_path,
                    person_folder = excluded.person_folder,
                    last_seen_path = excluded.last_seen_path,
                    updated_at = excluded.updated_at
                """,
                (
                    source_hash, source_filename, source_relative_path, person_folder,
                    page_count, ts, ts, PIPELINE_VERSION, source_relative_path, ts,
                ),
            )
            self._conn.execute(
                "DELETE FROM logical_documents WHERE source_hash = ?", (source_hash,)
            )

    def save_analysis(
        self,
        source_hash: str,
        *,
        documents: list[dict],
        taxonomy_version: str,
        analysis_schema_version: str,
    ) -> None:
        """PROCESSING -> ANALYZED_PENDING_APPLY (không review) hoặc REVIEW_REQUIRED
        (còn ít nhất một logical document REVIEW_PENDING). Ghi bảng
        `logical_documents` = kết quả phân loại + segmentation đã đóng băng.

        Mỗi dict trong `documents` cần các khoá: source_pages, type_id, confidence,
        document_date, date_confidence, title_short, segmentation_flags,
        classification_status ("AUTO"/"REVIEW"), classification_reasons. Tuỳ chọn
        (DEV POLICY CLOSURE): classification_kind (mặc định TAXONOMY), subtype
        (mặc định None), date_precision (mặc định DAY nếu có document_date, None
        nếu không - không tự suy "MONTH"/"YEAR" tại đây, việc đó là của
        resolve-review vì cần người xác nhận thứ đã đọc từ notes).
        """
        has_review = any(d["classification_status"] == "REVIEW" for d in documents)
        status = STATUS_REVIEW_REQUIRED if has_review else STATUS_ANALYZED_PENDING_APPLY
        ts = _now()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE sources SET
                    status = ?, taxonomy_version = ?, analysis_schema_version = ?,
                    last_error = NULL, updated_at = ?
                WHERE source_hash = ? AND status = 'PROCESSING'
                """,
                (status, taxonomy_version, analysis_schema_version, ts, source_hash),
            )
            if cur.rowcount != 1:
                raise PipelineError(
                    f"Lưu analysis thất bại cho hash {source_hash[:12]}…: "
                    "record không ở trạng thái PROCESSING."
                )
            self._conn.execute(
                "DELETE FROM logical_documents WHERE source_hash = ?", (source_hash,)
            )
            for d in documents:
                is_review = d["classification_status"] == "REVIEW"
                lid = logical_document_id(source_hash, d["source_pages"])
                title = (d.get("title_short") or None)
                if title and len(title) > _MAX_TITLE_LEN:
                    title = title[:_MAX_TITLE_LEN]
                classification_kind = d.get("classification_kind") or CLASSIFICATION_KIND_TAXONOMY
                duplicate_of = d.get("duplicate_of")
                normalized_date, date_precision = validate_classification_metadata(
                    classification_kind=classification_kind,
                    type_id=d.get("type_id"),
                    subtype=d.get("subtype"),
                    document_date=d.get("document_date"),
                    date_precision=d.get("date_precision"),
                    duplicate_of=duplicate_of,
                )
                self._conn.execute(
                    """
                    INSERT INTO logical_documents (
                        logical_document_id, source_hash, source_pages, type_id, confidence,
                        document_date, date_confidence, title_short, segmentation_flags,
                        classification_status, classification_reasons, resolution_status,
                        resolved_type_id, resolved_document_date, resolved_by, resolved_at,
                        current_target_filename, target_dir, sequence_index,
                        created_at, updated_at, classification_kind, subtype, date_precision,
                        duplicate_of, resolved_classification_kind, resolved_subtype,
                        resolved_date_precision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                        ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                    """,
                    (
                        lid, source_hash, json.dumps(d["source_pages"]), d["type_id"] or "UNKNOWN",
                        float(d["confidence"]), normalized_date,
                        float(d.get("date_confidence") or 0.0), title,
                        json.dumps(d.get("segmentation_flags") or []),
                        d["classification_status"], json.dumps(d.get("classification_reasons") or []),
                        RESOLUTION_REVIEW_PENDING if is_review else RESOLUTION_AUTO_RESOLVED,
                        ts, ts, classification_kind, d.get("subtype"), date_precision,
                    ),
                )

    def cached_analysis(
        self, source_hash: str, *, taxonomy_version: str, analysis_schema_version: str
    ) -> Optional[list[LogicalDocumentRow]]:
        """Trả về logical_documents đã lưu NẾU fingerprint còn khớp và nguồn đang
        ở ANALYZED_PENDING_APPLY/REVIEW_REQUIRED. None nếu không có cache hợp lệ
        (STALE_ANALYSIS hoặc chưa từng phân tích)."""
        rec = self.get(source_hash)
        if rec is None or rec.status not in (STATUS_ANALYZED_PENDING_APPLY, STATUS_REVIEW_REQUIRED):
            return None
        if rec.taxonomy_version != taxonomy_version or rec.analysis_schema_version != analysis_schema_version:
            return None
        return self.logical_documents_for(source_hash)

    def get_logical_document(self, logical_document_id_: str) -> Optional[LogicalDocumentRow]:
        row = self._conn.execute(
            "SELECT * FROM logical_documents WHERE logical_document_id = ?", (logical_document_id_,)
        ).fetchone()
        return _row_to_logical_document(row) if row else None

    def logical_documents_for(self, source_hash: str) -> list[LogicalDocumentRow]:
        rows = self._conn.execute(
            "SELECT * FROM logical_documents WHERE source_hash = ? ORDER BY logical_document_id",
            (source_hash,),
        ).fetchall()
        return [_row_to_logical_document(r) for r in rows]

    def logical_documents_for_person(
        self, person_folder: str, *, type_id: Optional[str] = None,
        include_retired: bool = True,
    ) -> list[LogicalDocumentRow]:
        """Toàn bộ logical_documents của MỌI nguồn đã biết của một người — nền
        tảng cho global cross-run naming (Phase F/G).

        Historical rows remain queryable by default.  Active workflow callers
        pass ``include_retired=False`` so tombstoned source history cannot
        consume naming slots or enter a new apply plan.
        """
        q = (
            "SELECT ld.* FROM logical_documents ld "
            "JOIN sources s ON s.source_hash = ld.source_hash "
            "WHERE s.person_folder = ?"
        )
        params: list = [person_folder]
        if type_id is not None:
            q += " AND (COALESCE(ld.resolved_type_id, ld.type_id) = ?)"
            params.append(type_id)
        if not include_retired:
            q += " AND NOT EXISTS ("
            q += "SELECT 1 FROM source_retirements sr WHERE sr.source_hash = ld.source_hash"
            q += ")"
        q += " ORDER BY ld.logical_document_id"
        rows = self._conn.execute(q, params).fetchall()
        return [_row_to_logical_document(r) for r in rows]

    def summarize_person(
        self, person_folder: str, present_hashes: Optional[set[str]] = None
    ) -> dict[str, int]:
        """Tóm tắt canonical state theo *effective* resolution.

        Báo cáo vận hành phải đếm ``resolved_classification_kind`` khi có,
        thay vì đếm nhãn phân loại thô trước khi operator resolve.  Cột review
        cũng chỉ đếm ``REVIEW_PENDING`` hiện tại; artifact review đã resolved
        (nếu còn được giữ để audit) được tách riêng.
        """
        rows = self.logical_documents_for_person(person_folder)
        lifecycle = self.source_lifecycle(person_folder, present_hashes)
        lifecycle_by_hash = {item.source_hash: item.lifecycle_status for item in lifecycle}
        active_rows = [
            row for row in rows
            if lifecycle_by_hash.get(row.source_hash) == SOURCE_ACTIVE
        ]
        missing_rows = [
            row for row in rows
            if lifecycle_by_hash.get(row.source_hash) == SOURCE_MISSING
        ]
        historical_rows = [
            row for row in rows
            if lifecycle_by_hash.get(row.source_hash) == SOURCE_RETIRED
        ]
        taxonomy = supporting = duplicate = 0
        review_pending = review_resolved = auto_resolved = 0
        historical_review_artifacts = 0
        for row in active_rows:
            kind = row.effective_classification_kind
            if kind == CLASSIFICATION_KIND_TAXONOMY:
                taxonomy += 1
            elif kind == "SUPPORTING_DOCUMENT":
                supporting += 1
            elif kind == CLASSIFICATION_KIND_DUPLICATE:
                duplicate += 1
            else:  # pragma: no cover - validate_classification_metadata chặn từ lúc ghi
                raise PipelineError(f"classification_kind không hợp lệ trong state: {kind!r}")

            if row.resolution_status == RESOLUTION_REVIEW_PENDING:
                review_pending += 1
            elif row.resolution_status == RESOLUTION_REVIEW_RESOLVED:
                review_resolved += 1
                if row.target_dir == "review" and row.current_target_filename:
                    historical_review_artifacts += 1
            elif row.resolution_status == RESOLUTION_AUTO_RESOLVED:
                auto_resolved += 1
            else:  # pragma: no cover - DB CHECK bảo vệ giá trị này
                raise PipelineError(f"resolution_status không hợp lệ trong state: {row.resolution_status!r}")

        # Missing sources remain a blocker and their unresolved reviews must
        # stay visible, but their classification is not counted as active
        # dossier content until the source is restored or explicitly retired.
        review_pending += sum(
            row.resolution_status == RESOLUTION_REVIEW_PENDING for row in missing_rows
        )

        for row in historical_rows:
            if row.resolution_status == RESOLUTION_REVIEW_RESOLVED and row.target_dir == "review" and row.current_target_filename:
                historical_review_artifacts += 1

        return {
            "logical_documents": len(rows),
            "active_logical_documents": len(active_rows),
            "missing_logical_documents": len(missing_rows),
            "historical_logical_documents": len(historical_rows),
            "taxonomy": taxonomy,
            "supporting": supporting,
            "duplicate": duplicate,
            "auto_resolved": auto_resolved,
            "review_resolved": review_resolved,
            "review_pending": review_pending,
            "historical_review_artifacts": historical_review_artifacts,
            "active_sources": sum(item.lifecycle_status == SOURCE_ACTIVE for item in lifecycle),
            "missing_sources": sum(item.lifecycle_status == SOURCE_MISSING for item in lifecycle),
            "retired_sources": sum(item.lifecycle_status == SOURCE_RETIRED for item in lifecycle),
            "active_pages": sum(item.page_count for item in lifecycle if item.lifecycle_status == SOURCE_ACTIVE),
            "missing_pages": sum(item.page_count for item in lifecycle if item.lifecycle_status == SOURCE_MISSING),
            "retired_pages": sum(item.page_count for item in lifecycle if item.lifecycle_status == SOURCE_RETIRED),
        }

    def hydrate_legacy_logical_documents(
        self,
        source_hash: str,
        *,
        source_filename: str,
        source_relative_path: str,
        person_folder: str,
        page_count: int,
        documents: list[dict],
        manifest_path: str,
        taxonomy_version: str,
        analysis_schema_version: str,
    ) -> LegacyHydrationResult:
        """Khôi phục metadata logical-document từ evidence legacy đã preflight.

        Hàm này không đọc PDF, không tạo artifact và không tự resolve REVIEW.
        Caller phải chứng minh identity/artifact trước khi gọi.  Dù vậy, hàm
        vẫn validate đầy đủ metadata và kiểm tra row đã có để một recovery
        partial không thể vô tình ghi đè state khác.  Mọi thay đổi được commit
        cùng một transaction.
        """
        source = self.get(source_hash)
        if source is not None:
            if source.person_folder != person_folder:
                raise PipelineError("Legacy recovery từ chối source hash thuộc person_folder khác.")
            if source.page_count != page_count:
                raise PipelineError("Legacy recovery từ chối source có page_count canonical khác inventory.")
        if not documents:
            raise PipelineError("Legacy recovery từ chối source không có logical document.")

        desired: dict[str, dict] = {}
        owned_pages: set[int] = set()
        for raw in documents:
            pages = raw.get("source_pages")
            if not isinstance(pages, list) or not pages or not all(
                isinstance(p, int) and not isinstance(p, bool) for p in pages
            ):
                raise PipelineError("Legacy logical document có source_pages không hợp lệ.")
            if pages != sorted(pages) or len(set(pages)) != len(pages):
                raise PipelineError("Legacy logical document có source_pages không theo thứ tự hoặc bị lặp.")
            if any(p < 1 or p > page_count for p in pages):
                raise PipelineError("Legacy logical document có trang ngoài phạm vi source.")
            if owned_pages.intersection(pages):
                raise PipelineError("Legacy logical documents bị overlap trang.")
            owned_pages.update(pages)

            expected_id = logical_document_id(source_hash, pages)
            supplied_id = raw.get("logical_document_id")
            if supplied_id is not None and supplied_id != expected_id:
                raise PipelineError(
                    f"LEGACY_LOGICAL_IDENTITY_AMBIGUOUS: {supplied_id!r} không khớp ID deterministic {expected_id!r}."
                )

            classification_status = raw.get("classification_status")
            if classification_status not in ("AUTO", "REVIEW"):
                raise PipelineError("Legacy logical document thiếu classification_status AUTO/REVIEW.")
            resolution_status = raw.get("resolution_status")
            if resolution_status not in (
                RESOLUTION_AUTO_RESOLVED,
                RESOLUTION_REVIEW_PENDING,
                RESOLUTION_REVIEW_RESOLVED,
            ):
                raise PipelineError("Legacy logical document có resolution_status không hợp lệ.")

            kind = raw.get("classification_kind") or CLASSIFICATION_KIND_TAXONOMY
            document_date, date_precision = validate_classification_metadata(
                classification_kind=kind,
                type_id=raw.get("type_id"),
                subtype=raw.get("subtype"),
                document_date=raw.get("document_date"),
                date_precision=raw.get("date_precision"),
                duplicate_of=raw.get("duplicate_of"),
            )
            resolved_kind = raw.get("resolved_classification_kind")
            resolved_type_id = raw.get("resolved_type_id")
            resolved_subtype = raw.get("resolved_subtype")
            resolved_date = raw.get("resolved_document_date")
            resolved_precision = raw.get("resolved_date_precision")
            if resolution_status == RESOLUTION_REVIEW_RESOLVED:
                resolved_date, resolved_precision = validate_classification_metadata(
                    classification_kind=resolved_kind or CLASSIFICATION_KIND_TAXONOMY,
                    type_id=resolved_type_id,
                    subtype=resolved_subtype,
                    document_date=resolved_date,
                    date_precision=resolved_precision,
                    duplicate_of=raw.get("duplicate_of"),
                )
            elif any(v is not None for v in (resolved_kind, resolved_type_id, resolved_subtype, resolved_date, resolved_precision)):
                raise PipelineError("Legacy logical document chưa resolved nhưng lại chứa resolved metadata.")

            target_dir = raw.get("target_dir")
            target_file = raw.get("current_target_filename")
            if target_dir not in ("output", "review") or not isinstance(target_file, str) or not target_file:
                raise PipelineError("Legacy logical document thiếu target artifact hợp lệ.")

            desired[expected_id] = {
                "logical_document_id": expected_id,
                "source_pages": pages,
                "type_id": raw.get("type_id") or "UNKNOWN",
                "confidence": float(raw.get("confidence") or 0.0),
                "document_date": document_date,
                "date_confidence": float(raw.get("date_confidence") or 0.0),
                "title_short": raw.get("title_short") or None,
                "segmentation_flags": list(raw.get("segmentation_flags") or []),
                "classification_status": classification_status,
                "classification_reasons": list(raw.get("classification_reasons") or []),
                "resolution_status": resolution_status,
                "resolved_type_id": resolved_type_id,
                "resolved_document_date": resolved_date,
                "resolved_by": raw.get("resolved_by"),
                "resolved_at": raw.get("resolved_at"),
                "current_target_filename": target_file,
                "target_dir": target_dir,
                "sequence_index": raw.get("sequence_index"),
                "classification_kind": kind,
                "subtype": raw.get("subtype"),
                "date_precision": date_precision,
                "duplicate_of": raw.get("duplicate_of"),
                "resolved_classification_kind": resolved_kind,
                "resolved_subtype": resolved_subtype,
                "resolved_date_precision": resolved_precision,
            }

        if owned_pages != set(range(1, page_count + 1)):
            raise PipelineError("Legacy logical documents không cover đúng toàn bộ trang nguồn.")

        existing = {row.logical_document_id: row for row in self.logical_documents_for(source_hash)}
        unexpected = sorted(set(existing) - set(desired))
        if unexpected:
            raise PipelineError(
                "Legacy recovery từ chối source đã có logical identity ngoài ledger: " + ", ".join(unexpected)
            )
        comparable = (
            "source_pages", "type_id", "document_date", "classification_status",
            "classification_reasons", "resolution_status", "current_target_filename",
            "target_dir", "sequence_index", "classification_kind", "subtype",
            "date_precision", "duplicate_of", "resolved_classification_kind",
            "resolved_type_id", "resolved_subtype", "resolved_document_date",
            "resolved_date_precision",
        )
        for lid, row in existing.items():
            now = row.as_dict()
            mismatch = []
            for field in comparable:
                expected = desired[lid][field]
                # as_dict export biểu diễn NULL bằng UNKNOWN để external state
                # không mơ hồ; DB vẫn phải giữ NULL cho metadata unresolved để
                # không che date_precision phân loại gốc.
                if field == "resolved_date_precision" and expected is None:
                    expected = DATE_PRECISION_UNKNOWN
                if now[field] != expected:
                    mismatch.append(field)
            if mismatch:
                raise PipelineError(
                    f"Legacy recovery conflict tại {lid}: state hiện có khác ledger ở {', '.join(mismatch)}."
                )

        restored = tuple(sorted(set(desired) - set(existing)))
        target_status = (
            STATUS_REVIEW_REQUIRED
            if any(d["resolution_status"] == RESOLUTION_REVIEW_PENDING for d in desired.values())
            else STATUS_PROCESSED
        )
        source_needs_update = (
            source is None
            or source.status != target_status
            or source.logical_document_count != len(desired)
            or source.manifest_path != manifest_path
            or source.taxonomy_version != taxonomy_version
            or source.analysis_schema_version != analysis_schema_version
            or (target_status == STATUS_REVIEW_REQUIRED and source.processed_at is not None)
        )
        if not restored and not source_needs_update:
            return LegacyHydrationResult(restored, target_status)

        ts = _now()
        with self._conn:
            if source is None:
                self._conn.execute(
                    """
                    INSERT INTO sources (
                        source_hash, source_filename, source_relative_path, person_folder,
                        page_count, status, first_seen_at, processing_started_at, processed_at,
                        logical_document_count, manifest_path, last_error, pipeline_version,
                        taxonomy_version, analysis_schema_version, last_seen_path, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_hash, source_filename, source_relative_path, person_folder,
                        page_count, target_status, ts,
                        ts if target_status == STATUS_PROCESSED else None,
                        len(desired), manifest_path, PIPELINE_VERSION, taxonomy_version,
                        analysis_schema_version, source_relative_path, ts,
                    ),
                )
            for lid in restored:
                d = desired[lid]
                self._conn.execute(
                    """
                    INSERT INTO logical_documents (
                        logical_document_id, source_hash, source_pages, type_id, confidence,
                        document_date, date_confidence, title_short, segmentation_flags,
                        classification_status, classification_reasons, resolution_status,
                        resolved_type_id, resolved_document_date, resolved_by, resolved_at,
                        current_target_filename, target_dir, sequence_index, created_at, updated_at,
                        classification_kind, subtype, date_precision, duplicate_of,
                        resolved_classification_kind, resolved_subtype, resolved_date_precision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lid, source_hash, json.dumps(d["source_pages"]), d["type_id"], d["confidence"],
                        d["document_date"], d["date_confidence"], d["title_short"],
                        json.dumps(d["segmentation_flags"]), d["classification_status"],
                        json.dumps(d["classification_reasons"]), d["resolution_status"],
                        d["resolved_type_id"], d["resolved_document_date"], d["resolved_by"],
                        d["resolved_at"], d["current_target_filename"], d["target_dir"],
                        d["sequence_index"], ts, ts, d["classification_kind"], d["subtype"],
                        d["date_precision"], d["duplicate_of"], d["resolved_classification_kind"],
                        d["resolved_subtype"], d["resolved_date_precision"],
                    ),
                )
            if source_needs_update:
                if source is not None:
                    self._conn.execute(
                        """
                        UPDATE sources SET
                            status = ?, processed_at = ?, logical_document_count = ?, manifest_path = ?,
                            last_error = NULL, pipeline_version = ?, taxonomy_version = ?,
                            analysis_schema_version = ?, last_seen_path = ?, updated_at = ?
                        WHERE source_hash = ?
                        """,
                        (
                            target_status,
                            source.processed_at if target_status == STATUS_PROCESSED else None,
                            len(desired), manifest_path, PIPELINE_VERSION, taxonomy_version,
                            analysis_schema_version, source_relative_path, ts, source_hash,
                        ),
                    )
        return LegacyHydrationResult(restored, target_status)

    def set_target(
        self, logical_document_id_: str, *, target_filename: str, target_dir: str, sequence_index: Optional[int]
    ) -> None:
        ts = _now()
        with self._conn:
            self._conn.execute(
                """
                UPDATE logical_documents SET
                    current_target_filename = ?, target_dir = ?, sequence_index = ?, updated_at = ?
                WHERE logical_document_id = ?
                """,
                (target_filename, target_dir, sequence_index, ts, logical_document_id_),
            )

    def resolve_review(
        self,
        logical_document_id_: str,
        *,
        resolved_by: str,
        resolved_classification_kind: Optional[str] = None,
        resolved_type_id: Optional[str] = None,
        resolved_subtype: Optional[str] = None,
        resolved_document_date: Optional[str] = None,
        resolved_date_precision: Optional[str] = None,
        duplicate_of: Optional[str] = None,
    ) -> None:
        """Người vận hành chốt một logical document đang REVIEW_PENDING.

        `resolved_classification_kind` mặc định giữ TAXONOMY (chỉ đổi type_id/
        ngày). DEV POLICY CLOSURE: có thể chốt sang SUPPORTING_DOCUMENT (không
        type_id) hoặc DUPLICATE (kèm `duplicate_of`). Validate nghiệp vụ (type_id
        hợp lệ, duplicate_of tồn tại...) là việc của app/review.py - hàm này chỉ
        ghi, KHÔNG tự động ghi file (apply lần sau tính lại naming - Phase E)."""
        kind = resolved_classification_kind or CLASSIFICATION_KIND_TAXONOMY
        if kind not in CLASSIFICATION_KINDS:
            raise PipelineError(f"resolved_classification_kind không hợp lệ: {kind!r}")
        if resolved_date_precision is not None and resolved_date_precision not in DATE_PRECISIONS:
            raise PipelineError(f"resolved_date_precision không hợp lệ: {resolved_date_precision!r}")
        validate_classification_metadata(
            classification_kind=kind,
            type_id=resolved_type_id,
            subtype=resolved_subtype,
            document_date=resolved_document_date,
            date_precision=resolved_date_precision,
            duplicate_of=duplicate_of,
        )
        ts = _now()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE logical_documents SET
                    resolution_status = 'REVIEW_RESOLVED',
                    resolved_classification_kind = ?, resolved_type_id = ?, resolved_subtype = ?,
                    resolved_document_date = ?, resolved_date_precision = ?, duplicate_of = ?,
                    resolved_by = ?, resolved_at = ?, updated_at = ?
                WHERE logical_document_id = ? AND resolution_status = 'REVIEW_PENDING'
                """,
                (
                    kind, resolved_type_id, resolved_subtype, resolved_document_date,
                    resolved_date_precision, duplicate_of, resolved_by, ts, ts, logical_document_id_,
                ),
            )
            if cur.rowcount != 1:
                raise PipelineError(
                    f"resolve_review thất bại cho {logical_document_id_}: "
                    "không tồn tại hoặc không ở REVIEW_PENDING."
                )

    def pending_reviews_for_source(self, source_hash: str) -> list[LogicalDocumentRow]:
        return [
            d for d in self.logical_documents_for(source_hash)
            if d.resolution_status == RESOLUTION_REVIEW_PENDING
        ]

    def commit_processed(
        self, source_hash: str, *, logical_document_count: int, manifest_path: str
    ) -> None:
        """-> PROCESSED. Chỉ gọi sau khi: apply hoàn tất + QC PASS + KHÔNG còn
        logical document nào REVIEW_PENDING của nguồn này."""
        pending = self.pending_reviews_for_source(source_hash)
        if pending:
            raise PipelineError(
                f"Không thể đánh PROCESSED cho hash {source_hash[:12]}…: "
                f"còn {len(pending)} logical document REVIEW_PENDING."
            )
        ts = _now()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE sources SET
                    status = 'PROCESSED', processed_at = ?, logical_document_count = ?,
                    manifest_path = ?, last_error = NULL, updated_at = ?
                WHERE source_hash = ? AND status IN ('ANALYZED_PENDING_APPLY', 'REVIEW_REQUIRED')
                """,
                (ts, logical_document_count, manifest_path, ts, source_hash),
            )
            if cur.rowcount != 1:
                raise PipelineError(
                    f"State transition -> PROCESSED thất bại cho hash {source_hash[:12]}…: "
                    "record không ở ANALYZED_PENDING_APPLY/REVIEW_REQUIRED."
                )

    def mark_failed(self, source_hash: str, *, error: str) -> None:
        """-> FAILED. Không tự retry; cần hành động rõ ràng của người vận hành."""
        ts = _now()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE sources SET status = 'FAILED', last_error = ?, updated_at = ?
                WHERE source_hash = ? AND status IN
                    ('PROCESSING', 'ANALYZED_PENDING_APPLY', 'REVIEW_REQUIRED')
                """,
                ((error or "")[:_MAX_ERROR_LEN], ts, source_hash),
            )
            if cur.rowcount != 1:
                raise PipelineError(
                    f"State transition -> FAILED thất bại cho hash {source_hash[:12]}…: "
                    "record không ở trạng thái hợp lệ để chuyển FAILED."
                )

    # ================= migration (Phase 27 cũ) =================
    def import_processed(
        self,
        *,
        source_hash: str,
        source_filename: str,
        source_relative_path: str,
        person_folder: str,
        page_count: int,
        logical_document_count: int,
        manifest_path: str,
    ) -> None:
        """Nạp một record PROCESSED từ bằng chứng manifest/output đã xác minh.

        CHỈ dùng bởi `app.state_import` sau khi đã kiểm tra: manifest tồn tại,
        hash/pages khớp, mọi target_file thực sự có trên đĩa. Không có
        logical_documents chi tiết (import chỉ có bằng chứng ở mức file, không
        có lại được segmentation/classification gốc) — nguồn import được coi
        đã hoàn tất nghiệp vụ (không còn review treo) vì bằng chứng là một
        ledger apply THÀNH CÔNG trước đó.
        """
        ts = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sources (
                    source_hash, source_filename, source_relative_path, person_folder,
                    page_count, status, first_seen_at, processing_started_at,
                    processed_at, logical_document_count, manifest_path, last_error,
                    pipeline_version, taxonomy_version, analysis_schema_version,
                    last_seen_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PROCESSED', ?, NULL, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)
                ON CONFLICT(source_hash) DO NOTHING
                """,
                (
                    source_hash, source_filename, source_relative_path, person_folder,
                    page_count, ts, ts, logical_document_count, manifest_path,
                    PIPELINE_VERSION, source_relative_path, ts,
                ),
            )
