from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .db import Database
from .taxonomy import TaxonomyAdapter


PIPELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_documents (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  source_file TEXT NOT NULL,
  source_pages_json TEXT NOT NULL,
  type_id TEXT,
  status TEXT,
  target_file TEXT,
  title_short TEXT,
  document_date TEXT,
  review_reason TEXT,
  provider TEXT NOT NULL,
  raw_path TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(case_id,source_file,source_pages_json)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_case ON pipeline_documents(case_id);
"""


@dataclass
class IntegrationResult:
    provider: str
    source_path: str | None = None
    entries: list[dict] = field(default_factory=list)
    warnings: list[tuple[str, str, str]] = field(default_factory=list)
    available: bool = False


class NoopProvider:
    name = "noop"

    def load_case(self, folder_name: str) -> IntegrationResult:
        return IntegrationResult(provider=self.name)


class ManifestProvider:
    name = "manifest"

    def __init__(self, root_or_file: Path, taxonomy: TaxonomyAdapter):
        self.root_or_file = root_or_file
        self.taxonomy = taxonomy

    def _candidate(self, folder_name: str) -> Path | None:
        if self.root_or_file.is_file():
            return self.root_or_file
        candidates = [
            self.root_or_file / folder_name / "_manifest.json",
            self.root_or_file / folder_name / "manifest.apply.json",
            self.root_or_file / folder_name / "manifest.dryrun.json",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def load_case(self, folder_name: str) -> IntegrationResult:
        path = self._candidate(folder_name)
        if path is None:
            return IntegrationResult(provider=self.name, source_path=str(self.root_or_file), available=False)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return IntegrationResult(provider=self.name, source_path=str(path), warnings=[("CAN_XAC_MINH", "ERROR", f"Manifest không đọc được: {exc}")], available=True)
        if not isinstance(raw, dict) or not isinstance(raw.get("documents"), list):
            return IntegrationResult(provider=self.name, source_path=str(path), warnings=[("CAN_XAC_MINH", "ERROR", "Manifest không đúng schema documents[]")], available=True)
        entries: list[dict] = []
        warnings: list[tuple[str, str, str]] = []
        for item in raw["documents"]:
            if not isinstance(item, dict):
                continue
            type_id = item.get("type_id")
            if type_id not in (None, "UNKNOWN") and not self.taxonomy.is_valid(str(type_id)):
                warnings.append(("CAN_XAC_MINH", "ERROR", f"Manifest có type_id ngoài catalog: {type_id}"))
                continue
            entry = {
                "source_file": str(item.get("source_file", "")),
                "source_pages": item.get("source_pages") or [],
                "type_id": str(type_id) if type_id is not None else None,
                "status": item.get("status") or item.get("classification_status") or "UNKNOWN",
                "target_file": item.get("target_file"),
                "title_short": item.get("title_short"),
                "document_date": item.get("document_date"),
                "review_reason": item.get("review_reason"),
            }
            entries.append(entry)
            if entry["status"] == "REVIEW" or item.get("needs_review") or item.get("classification_status") == "REVIEW":
                warnings.append(("REVIEW_PENDING", "WARNING", f"Pipeline review pending: {entry['source_file']} trang {entry['source_pages']}"))
        return IntegrationResult(provider=self.name, source_path=str(path), entries=entries, warnings=warnings, available=True)


def provider_for(settings: Settings, taxonomy: TaxonomyAdapter):
    path = settings.manifest_path or settings.ledger_path
    return ManifestProvider(path, taxonomy) if path else NoopProvider()


def ensure_schema(db: Database) -> None:
    with db.session() as conn:
        conn.executescript(PIPELINE_SCHEMA)


def integrate_case(db: Database, case_id: int, provider, at: str) -> IntegrationResult:
    ensure_schema(db)
    row = db.one("SELECT folder_name FROM cases WHERE id=?", (case_id,))
    if row is None:
        raise ValueError("Không tìm thấy hồ sơ")
    result = provider.load_case(row["folder_name"])
    if not result.available and result.provider == "noop":
        return result
    with db.session() as conn:
        conn.execute("UPDATE warnings SET active=0,updated_at=? WHERE case_id=? AND warning_type IN ('REVIEW_PENDING','CAN_XAC_MINH')", (at, case_id))
        conn.execute("DELETE FROM pipeline_documents WHERE case_id=?", (case_id,))
        for entry in result.entries:
            pages = json.dumps(entry["source_pages"], ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """INSERT INTO pipeline_documents(case_id,source_file,source_pages_json,type_id,status,target_file,title_short,document_date,review_reason,provider,raw_path,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case_id, entry["source_file"], pages, entry["type_id"], entry["status"], entry["target_file"], entry["title_short"], entry["document_date"], json.dumps(entry["review_reason"], ensure_ascii=False) if entry["review_reason"] else None, result.provider, result.source_path, at),
            )
        for kind, severity, message in result.warnings:
            fingerprint = f"{case_id}:integration:{kind}:{message}"
            conn.execute(
                """INSERT INTO warnings(case_id,warning_type,severity,message,active,fingerprint,created_at,updated_at)
                   VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET active=1,updated_at=excluded.updated_at""",
                (case_id, kind, severity, message, fingerprint, at, at),
            )
    return result
