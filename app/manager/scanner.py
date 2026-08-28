from __future__ import annotations

import fnmatch
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from ..pdf_inventory import sha256_file
from .config import Settings
from .db import Database
from .parser import FolderMetadata, FilenameMetadata, parse_folder_name, parse_pdf_filename
from .taxonomy import TaxonomyAdapter


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class ScanResult:
    run_id: int | None = None
    status: str = "SUCCESS"
    folders_seen: int = 0
    files_seen: int = 0
    cases_created: int = 0
    cases_updated: int = 0
    docs_created: int = 0
    docs_updated: int = 0
    warnings_created: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {**self.__dict__, "error_messages": list(self.error_messages)}


class ScanService:
    def __init__(self, settings: Settings, db: Database, taxonomy: TaxonomyAdapter | None = None):
        self.settings = settings
        self.db = db
        self.taxonomy = taxonomy or TaxonomyAdapter.load()

    def scan(self, case_id: int | None = None) -> ScanResult:
        started = time.perf_counter()
        result = ScanResult()
        started_at = utc_now()
        self.taxonomy.seed(self.db)
        with self.db.session() as conn:
            result.run_id = conn.execute("INSERT INTO scan_runs(started_at,status) VALUES(?,?)", (started_at, "RUNNING")).lastrowid
            root = self.settings.data_root.resolve()
            try:
                folders = self._folders(root, case_id, conn)
                seen_keys: set[str] = set()
                for folder in folders:
                    key = folder.relative_to(root).as_posix()
                    seen_keys.add(key)
                    self._scan_folder(conn, root, folder, result)
                    result.folders_seen += 1
                if case_id is None:
                    self._reconcile_missing(conn, seen_keys, result)
                result.status = "SUCCESS"
            except Exception as exc:
                result.status = "ERROR"
                result.errors += 1
                result.error_messages.append(str(exc))
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            summary = json.dumps(result.as_dict(), ensure_ascii=False)
            conn.execute(
                """UPDATE scan_runs SET ended_at=?,status=?,folders_seen=?,files_seen=?,cases_created=?,cases_updated=?,
                   docs_created=?,docs_updated=?,warnings_created=?,errors=?,duration_ms=?,summary_json=? WHERE id=?""",
                (utc_now(), result.status, result.folders_seen, result.files_seen, result.cases_created, result.cases_updated,
                 result.docs_created, result.docs_updated, result.warnings_created, result.errors, result.duration_ms, summary, result.run_id),
            )
        return result

    def _folders(self, root: Path, case_id: int | None, conn) -> list[Path]:
        if case_id is not None:
            row = conn.execute("SELECT folder_path FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                raise ValueError("Không tìm thấy hồ sơ")
            folder = Path(row["folder_path"]).resolve()
            self._assert_inside(root, folder)
            return [folder] if folder.is_dir() else []
        if not root.is_dir():
            raise ValueError(f"data_root không tồn tại hoặc không phải thư mục: {root}")
        return [p for p in sorted(root.iterdir(), key=lambda x: x.name.casefold()) if p.is_dir() and not self._ignored(p.name)]

    def _scan_folder(self, conn, root: Path, folder: Path, result: ScanResult) -> None:
        metadata = parse_folder_name(folder.name)
        case_key = folder.relative_to(root).as_posix()
        now = utc_now()
        row = conn.execute("SELECT * FROM cases WHERE case_key=?", (case_key,)).fetchone()
        fields = (metadata.m1, metadata.m2, metadata.m3, metadata.m4, metadata.m5, metadata.citizen_id,
                  metadata.person_name_raw, metadata.person_name_display, metadata.unit_code)
        if row is None:
            case_id = conn.execute(
                """INSERT INTO cases(case_key,folder_path,folder_name,m1,m2,m3,m4,m5,citizen_id,person_name_raw,
                   person_name_display,unit_code,auto_status,effective_status,is_present,first_seen_at,last_seen_at,last_scanned_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case_key, str(folder), folder.name, *fields, "CHUA_XU_LY", "CHUA_XU_LY", 1, now, now, now),
            ).lastrowid
            result.cases_created += 1
        else:
            case_id = row["id"]
            conn.execute(
                """UPDATE cases SET folder_path=?,folder_name=?,m1=?,m2=?,m3=?,m4=?,m5=?,citizen_id=?,person_name_raw=?,
                   person_name_display=?,unit_code=?,is_present=1,last_seen_at=?,last_scanned_at=? WHERE id=?""",
                (str(folder), folder.name, *fields, now, now, case_id),
            )
            result.cases_updated += 1

        conn.execute("UPDATE warnings SET active=0,updated_at=? WHERE case_id=?", (now, case_id))
        pdfs = self._pdfs(root, folder)
        seen_paths: set[str] = set()
        parsed_by_doc: list[tuple[int, FilenameMetadata]] = []
        for path in pdfs:
            result.files_seen += 1
            rel = path.relative_to(root).as_posix()
            seen_paths.add(rel)
            parsed = parse_pdf_filename(path.name, self.taxonomy)
            stat = path.stat()
            existing = conn.execute("SELECT * FROM documents WHERE relative_path=?", (rel,)).fetchone()
            unchanged = bool(existing and existing["size_bytes"] == stat.st_size and existing["mtime_ns"] == stat.st_mtime_ns and existing["is_present"])
            sha = existing["sha256"] if unchanged else sha256_file(path)
            page_count = existing["page_count"] if unchanged else None
            readable = True
            if not unchanged:
                try:
                    page_count = len(PdfReader(str(path)).pages)
                    if page_count < 1:
                        readable = False
                except Exception:
                    readable = False
            parse_status = parsed.status if readable else "FILE_KHONG_DOC_DUOC"
            if existing is None:
                doc_id = conn.execute(
                    """INSERT INTO documents(case_id,relative_path,filename,taxonomy_code,taxonomy_name,sequence_no,priority,
                       size_bytes,mtime_ns,sha256,page_count,parse_status,is_present,first_seen_at,last_seen_at,last_hashed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (case_id, rel, path.name, parsed.taxonomy_code, self._name(parsed), parsed.sequence_no,
                     self._priority(parsed), stat.st_size, stat.st_mtime_ns, sha, page_count, parse_status, 1, now, now, now if not unchanged else None),
                ).lastrowid
                result.docs_created += 1
            else:
                doc_id = existing["id"]
                conn.execute(
                    """UPDATE documents SET case_id=?,filename=?,taxonomy_code=?,taxonomy_name=?,sequence_no=?,priority=?,
                       size_bytes=?,mtime_ns=?,sha256=?,page_count=?,parse_status=?,is_present=1,last_seen_at=?,last_hashed_at=? WHERE id=?""",
                    (case_id, path.name, parsed.taxonomy_code, self._name(parsed), parsed.sequence_no, self._priority(parsed),
                     stat.st_size, stat.st_mtime_ns, sha, page_count, parse_status, now, now if not unchanged else existing["last_hashed_at"], doc_id),
                )
                result.docs_updated += 1
            parsed_by_doc.append((doc_id, parsed))

        for old in conn.execute("SELECT id,relative_path FROM documents WHERE case_id=?", (case_id,)).fetchall():
            if old["relative_path"] not in seen_paths:
                conn.execute("UPDATE documents SET is_present=0,last_seen_at=? WHERE id=?", (now, old["id"]))

        if not metadata.valid:
            result.warnings_created += self._warning(conn, case_id, None, "SAI_TEN_THU_MUC", "WARNING", f"Tên thư mục không theo mẫu: {folder.name}", now)
        for doc_id, parsed in parsed_by_doc:
            if parsed.status == "MALFORMED_NAME":
                result.warnings_created += self._warning(conn, case_id, doc_id, "SAI_TEN_FILE", "WARNING", f"Tên file không theo mẫu: {parsed.filename}", now)
            elif parsed.status == "FILE_NGOAI_TAXONOMY":
                result.warnings_created += self._warning(conn, case_id, doc_id, "FILE_NGOAI_TAXONOMY", "WARNING", f"Mã tài liệu ngoài taxonomy: {parsed.taxonomy_code}", now)
            elif parsed.status == "OK" and not parsed.canonical_match:
                result.warnings_created += self._warning(conn, case_id, doc_id, "SAI_TEN_FILE", "WARNING", f"Tên file khác tên chuẩn catalog: {parsed.filename}", now)
            row_doc = conn.execute("SELECT sha256 FROM documents WHERE id=?", (doc_id,)).fetchone()
            if row_doc and row_doc["sha256"]:
                same = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE case_id=? AND sha256=? AND is_present=1", (case_id, row_doc["sha256"])).fetchone()["n"]
                if same > 1:
                    result.warnings_created += self._warning(conn, case_id, doc_id, "TRUNG_TAI_LIEU", "WARNING", "Có file trùng SHA-256 trong cùng hồ sơ.", now)
            status_row = conn.execute("SELECT parse_status FROM documents WHERE id=?", (doc_id,)).fetchone()
            if status_row and status_row["parse_status"] == "FILE_KHONG_DOC_DUOC":
                result.warnings_created += self._warning(conn, case_id, doc_id, "FILE_KHONG_DOC_DUOC", "ERROR", "Không đọc được PDF.", now)

        document_count = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE case_id=? AND is_present=1 AND parse_status='OK' AND taxonomy_code IS NOT NULL", (case_id,)).fetchone()["n"]
        warning_count = conn.execute("SELECT COUNT(*) AS n FROM warnings WHERE case_id=? AND active=1", (case_id,)).fetchone()["n"]
        conn.execute("UPDATE cases SET document_count=?,warning_count=? WHERE id=?", (document_count, warning_count, case_id))

    def _reconcile_missing(self, conn, seen_keys: set[str], result: ScanResult) -> None:
        now = utc_now()
        for row in conn.execute("SELECT id,case_key FROM cases WHERE is_present=1").fetchall():
            if row["case_key"] not in seen_keys:
                conn.execute("UPDATE cases SET is_present=0,last_scanned_at=? WHERE id=?", (now, row["id"]))
                conn.execute("UPDATE documents SET is_present=0,last_seen_at=? WHERE case_id=?", (now, row["id"]))

    def _pdfs(self, root: Path, folder: Path) -> list[Path]:
        out: list[Path] = []
        for current, dirs, files in os.walk(folder, followlinks=False):
            current_path = Path(current)
            dirs[:] = [d for d in dirs if not self._ignored(d) and self._inside(root, current_path / d)]
            for name in files:
                if self._ignored(name) or not name.lower().endswith(".pdf"):
                    continue
                path = current_path / name
                if self._inside(root, path.resolve()) and not path.is_symlink():
                    out.append(path)
        return sorted(out, key=lambda x: x.relative_to(root).as_posix().casefold())

    def _warning(self, conn, case_id: int, doc_id: int | None, kind: str, severity: str, message: str, now: str) -> int:
        fingerprint = f"{case_id}:{doc_id or 0}:{kind}:{message}"
        existing = conn.execute("SELECT id FROM warnings WHERE fingerprint=?", (fingerprint,)).fetchone()
        conn.execute(
            """INSERT INTO warnings(case_id,document_id,warning_type,severity,message,active,fingerprint,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET active=1,updated_at=excluded.updated_at""",
            (case_id, doc_id, kind, severity, message, fingerprint, now, now),
        )
        return 0 if existing else 1

    def _name(self, parsed: FilenameMetadata) -> str | None:
        item = self.taxonomy.get(parsed.taxonomy_code or "")
        return item.name if item else None

    def _priority(self, parsed: FilenameMetadata) -> int | None:
        item = self.taxonomy.get(parsed.taxonomy_code or "")
        return item.priority if item else None

    def _ignored(self, name: str) -> bool:
        return name.startswith(".") or any(fnmatch.fnmatch(name, pattern) for pattern in self.settings.ignore_patterns)

    @staticmethod
    def _inside(root: Path, candidate: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    def _assert_inside(self, root: Path, candidate: Path) -> None:
        if not self._inside(root, candidate):
            raise ValueError("Đường dẫn nằm ngoài data_root")
