from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY,
  case_key TEXT UNIQUE NOT NULL,
  folder_path TEXT UNIQUE NOT NULL,
  folder_name TEXT NOT NULL,
  m1 TEXT, m2 TEXT, m3 TEXT, m4 TEXT, m5 TEXT,
  citizen_id TEXT,
  person_name_raw TEXT,
  person_name_display TEXT,
  unit_code TEXT,
  auto_status TEXT NOT NULL DEFAULT 'CHUA_XU_LY',
  manual_status TEXT,
  effective_status TEXT NOT NULL DEFAULT 'CHUA_XU_LY',
  progress_percent REAL NOT NULL DEFAULT 0,
  document_count INTEGER NOT NULL DEFAULT 0,
  warning_count INTEGER NOT NULL DEFAULT 0,
  missing_priority1_count INTEGER NOT NULL DEFAULT 0,
  is_present INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_scanned_at TEXT,
  completed_at TEXT,
  reviewed_by TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  relative_path TEXT UNIQUE NOT NULL,
  filename TEXT NOT NULL,
  taxonomy_code TEXT,
  taxonomy_name TEXT,
  sequence_no INTEGER,
  priority INTEGER,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  page_count INTEGER,
  parse_status TEXT NOT NULL DEFAULT 'UNKNOWN',
  is_present INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_hashed_at TEXT
);
CREATE TABLE IF NOT EXISTS taxonomy_items (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 3,
  active INTEGER NOT NULL DEFAULT 1,
  default_applicability TEXT NOT NULL DEFAULT 'CHUA_XAC_DINH'
);
CREATE TABLE IF NOT EXISTS checklist_overrides (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  taxonomy_code TEXT NOT NULL,
  status TEXT NOT NULL,
  note TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(case_id, taxonomy_code)
);
CREATE TABLE IF NOT EXISTS warnings (
  id INTEGER PRIMARY KEY,
  case_id INTEGER REFERENCES cases(id) ON DELETE CASCADE,
  document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  warning_type TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'WARNING',
  message TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  fingerprint TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_history (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  folders_seen INTEGER NOT NULL DEFAULT 0,
  files_seen INTEGER NOT NULL DEFAULT 0,
  cases_created INTEGER NOT NULL DEFAULT 0,
  cases_updated INTEGER NOT NULL DEFAULT 0,
  docs_created INTEGER NOT NULL DEFAULT 0,
  docs_updated INTEGER NOT NULL DEFAULT 0,
  warnings_created INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER,
  summary_json TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_case ON documents(case_id);
CREATE INDEX IF NOT EXISTS idx_documents_taxonomy ON documents(taxonomy_code);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_present ON documents(is_present);
CREATE INDEX IF NOT EXISTS idx_warnings_case_active ON warnings(case_id, active);
CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(effective_status);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
    def backup_to(self, target: str | Path) -> Path:
        """Create a consistent SQLite metadata-only backup."""
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        try:
            backup = sqlite3.connect(destination)
            try:
                source.backup(backup)
                backup.commit()
            finally:
                backup.close()
        finally:
            source.close()
        return destination


    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

