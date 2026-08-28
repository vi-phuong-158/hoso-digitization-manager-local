from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.main import create_app
from tests.manager.test_scanner import make_pdf


def test_scan_list_detail_and_actions(tmp_path: Path):
    root = tmp_path / "input"; root.mkdir()
    folder = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"; folder.mkdir()
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    client.get("/")
    token = client.cookies.get("csrf_token")
    assert token
    scan = client.post("/scan", headers={"X-CSRF-Token": token})
    assert scan.status_code == 200
    assert scan.json()["folders_seen"] == 1
    listing = client.get("/cases?format=json")
    assert listing.status_code == 200
    case_id = listing.json()["items"][0]["id"]
    detail = client.get(f"/cases/{case_id}?format=json")
    assert detail.status_code == 200
    assert detail.json()["case"]["citizen_id"] == "012345678901"
    override = client.post(f"/cases/{case_id}/checklist/02", json={"status": "KHONG_PHAT_SINH"}, headers={"X-CSRF-Token": token})
    assert override.status_code == 200
    complete = client.post(f"/cases/{case_id}/complete", json={"reviewed_by": "operator"}, headers={"X-CSRF-Token": token})
    assert complete.status_code == 200
    assert client.get(f"/cases/{case_id}?format=json").json()["case"]["effective_status"] == "HOAN_THANH"
    assert client.post(f"/cases/{case_id}/reopen", headers={"X-CSRF-Token": token}).status_code == 200


def test_open_document_does_not_accept_arbitrary_path(tmp_path: Path):
    root = tmp_path / "input"; root.mkdir()
    settings = Settings(data_root=root, database_path=tmp_path / "db.sqlite")
    client = TestClient(create_app(settings))
    assert client.get("/open/document/999").status_code == 404
