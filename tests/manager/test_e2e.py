from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.main import create_app
from app.manager.status import mark_complete
from tests.manager.fixtures import build_fixture_tree


def test_offline_pilot_flow_scan_dashboard_filter_detail(tmp_path: Path):
    root = tmp_path / "pilot-data"
    fixtures = build_fixture_tree(root)
    settings = Settings(data_root=root, database_path=tmp_path / "manager.db")
    client = TestClient(create_app(settings))
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    token = client.cookies.get("csrf_token")
    scan = client.post("/scan", headers={"X-CSRF-Token": token})
    assert scan.status_code == 200
    summary = scan.json()
    assert summary["folders_seen"] == 10
    assert summary["files_seen"] == 10
    assert client.get("/cases?format=json&q=Nguyen").json()["count"] == 1
    duplicate = client.get("/cases?format=json&warning=1").json()
    assert duplicate["count"] >= 3
    case_rows = client.get("/cases?format=json").json()["items"]
    completed_id = next(row["id"] for row in case_rows if row["folder_name"].endswith("Vu_Van_I"))
    assert client.post(f"/cases/{completed_id}/complete", headers={"X-CSRF-Token": token}).status_code == 200
    fixture_pdf = fixtures["completed"] / "01.Ly_lich_nguoi_xin_vao_dang.pdf"
    fixture_pdf.write_bytes(fixture_pdf.read_bytes() + b"\n")
    assert client.post("/scan", headers={"X-CSRF-Token": token}).status_code == 200
    detail = client.get(f"/cases/{completed_id}?format=json").json()
    assert detail["case"]["effective_status"] == "HOAN_THANH"
    assert any(item["warning_type"] == "CHANGED_AFTER_COMPLETION" for item in detail["warnings"])


def test_no_external_assets_in_ui(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "input", database_path=tmp_path / "db.sqlite")
    client = TestClient(create_app(settings))
    html = client.get("/").text.lower()
    assert "cdn" not in html
    assert "http://" not in html
    assert "https://" not in html
