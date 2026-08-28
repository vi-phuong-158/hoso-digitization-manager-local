from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.main import create_app
from app.pdf_inventory import sha256_file
from app.state import StateRegistry
from tests.manager.test_scanner import make_pdf


def test_review_ui_starts_a_non_mutating_session(tmp_path: Path):
    root = tmp_path / "input"
    folder = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"; folder.mkdir(parents=True)
    source = folder / "a.pdf"; make_pdf(source)
    state_db = tmp_path / "state" / "processing_state.db"
    with StateRegistry(state_db) as registry:
        source_hash = sha256_file(source)
        registry.begin_processing(source_hash=source_hash, source_filename="a.pdf", source_relative_path=f"{folder.name}/a.pdf", person_folder=folder.name, page_count=1)
        registry.save_analysis(source_hash, documents=[{
            "source_pages": [1], "type_id": "05", "confidence": 0.99, "document_date": "2020-01-01", "date_confidence": 0.99,
            "title_short": "QĐ", "segmentation_flags": [], "classification_status": "AUTO", "classification_reasons": [],
            "classification_kind": "TAXONOMY", "subtype": None, "date_precision": None,
        }], taxonomy_version="t", analysis_schema_version="1")
    client = TestClient(create_app(Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")))
    client.get("/"); token = client.cookies.get("csrf_token")
    assert client.post("/scan", headers={"X-CSRF-Token": token}).status_code == 200
    case_id = client.get("/cases?format=json").json()["items"][0]["id"]
    assert client.get("/reviews").status_code == 200
    page = client.get(f"/reviews/{case_id}")
    assert page.status_code == 200
    assert "Tài liệu nguồn không bị thay đổi" in page.text
    started = client.post(f"/reviews/{case_id}/start", headers={"X-CSRF-Token": token})
    assert started.status_code == 200
    assert "session_id" in started.json()
    detail = client.get(f"/reviews/{case_id}?format=json").json()
    assert detail["state_available"] is True
    assert len(detail["sessions"]) == 1
    semantic = client.post(f"/reviews/{case_id}/start-semantic", headers={"X-CSRF-Token": token})
    assert semantic.status_code == 409
    assert "chưa được cấu hình" in semantic.json()["detail"]
