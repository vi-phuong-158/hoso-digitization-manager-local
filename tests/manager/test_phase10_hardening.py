from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.db import Database
from app.manager.entrypoint import acquire_single_instance
from app.manager.main import create_app
from app.manager.scanner import ScanService
from app.manager.taxonomy import TaxonomyAdapter
from tests.manager.test_scanner import make_pdf


def _client(root: Path, database: Path, manifest: Path | None = None) -> TestClient:
    settings = Settings(data_root=root, database_path=database, manifest_path=manifest)
    return TestClient(create_app(settings))


def _scan(client: TestClient) -> int:
    client.get("/")
    token = client.cookies.get("csrf_token")
    assert token
    response = client.post("/scan", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    return client.get("/cases?format=json").json()["items"][0]["id"]


def test_state_persists_across_restart_and_changed_after_completion(tmp_path: Path):
    root = tmp_path / "input"
    folder = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"
    folder.mkdir(parents=True)
    source = folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf"
    make_pdf(source)
    db_path = tmp_path / "data" / "manager.db"

    first = _client(root, db_path)
    case_id = _scan(first)
    token = first.cookies.get("csrf_token")
    assert first.post(f"/cases/{case_id}/checklist/02", json={"status": "KHONG_PHAT_SINH"}, headers={"X-CSRF-Token": token}).status_code == 200
    assert first.post(f"/cases/{case_id}/complete", json={"reviewed_by": "pilot"}, headers={"X-CSRF-Token": token}).status_code == 200

    restarted = _client(root, db_path)
    assert restarted.get(f"/cases/{case_id}?format=json").json()["case"]["effective_status"] == "HOAN_THANH"
    source.write_bytes(source.read_bytes() + b"% changed-after-completion")
    token = restarted.cookies.get("csrf_token") or restarted.get("/").cookies.get("csrf_token")
    restarted.get("/")
    token = restarted.cookies.get("csrf_token")
    assert restarted.post("/scan", headers={"X-CSRF-Token": token}).status_code == 200
    detail = restarted.get(f"/cases/{case_id}?format=json").json()
    assert any(w["warning_type"] == "CHANGED_AFTER_COMPLETION" for w in detail["warnings"])


def test_manifest_evidence_and_metadata_backup_restore(tmp_path: Path):
    root = tmp_path / "input"
    folder = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"
    folder.mkdir(parents=True)
    make_pdf(folder / "opaque-source.pdf")
    output = tmp_path / "output" / folder.name
    output.mkdir(parents=True)
    (output / "_manifest.json").write_text(json.dumps({"documents": [{"source_file": "opaque-source.pdf", "source_pages": [1], "type_id": "86", "status": "REVIEW", "needs_review": True}]}), encoding="utf-8")
    db_path = tmp_path / "data" / "manager.db"
    client = _client(root, db_path, output.parent)
    case_id = _scan(client)
    detail = client.get(f"/cases/{case_id}?format=json").json()
    assert detail["pipeline_documents"][0]["type_id"] == "86"
    assert detail["case"]["document_count"] == 1
    assert detail["case"]["effective_status"] == "DANG_SO_HOA"
    backup = Database(db_path).backup_to(tmp_path / "backup.sqlite")
    assert backup.is_file()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
    restored = tmp_path / "restored.sqlite"
    restored.write_bytes(backup.read_bytes())
    assert Database(restored).one("SELECT COUNT(*) AS n FROM cases")["n"] == 1


def test_path_traversal_and_invalid_pdf_are_fail_safe(tmp_path: Path):
    root = tmp_path / "input"
    folder = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"
    folder.mkdir(parents=True)
    (folder / "not-a-pdf.pdf").write_bytes(b"not a PDF")
    client = _client(root, tmp_path / "db.sqlite")
    case_id = _scan(client)
    assert client.get("/open/document/../../requirements.txt").status_code in {404, 422}
    assert client.get("/open/document/999999").status_code == 404
    detail = client.get(f"/cases/{case_id}?format=json").json()
    assert detail["case"]["warning_count"] >= 1


def test_single_instance_is_non_network_and_testable(tmp_path: Path):
    # The Windows packaged path is exercised by the Phase 10 executable smoke.
    handle = acquire_single_instance(tmp_path)
    assert handle is not False
    if handle is not None:
        handle.close()

