from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.main import create_app

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "manager" / "templates"


def _client(tmp_path: Path) -> tuple[TestClient, str]:
    root = tmp_path / "input"
    root.mkdir()
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    return client, token


def test_get_backup_page_never_creates_a_file(tmp_path: Path):
    client, _ = _client(tmp_path)
    before = set((tmp_path / "data").glob("*.sqlite")) if (tmp_path / "data").is_dir() else set()
    resp = client.get("/backup")
    assert resp.status_code == 200
    after = set((tmp_path / "data").glob("*.sqlite"))
    assert after == before, "GET /backup must be read-only, per P1-12"

    json_resp = client.get("/backup", headers={"Accept": "application/json"})
    assert json_resp.status_code == 200
    after_json = set((tmp_path / "data").glob("*.sqlite"))
    assert after_json == before, "GET /backup?format=json must also be read-only"


def test_post_backup_requires_csrf(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post("/backup")
    assert resp.status_code == 403


def test_two_backups_produce_two_distinct_files(tmp_path: Path):
    client, token = _client(tmp_path)
    first = client.post("/backup", headers={"X-CSRF-Token": token})
    time.sleep(0.01)
    second = client.post("/backup", headers={"X-CSRF-Token": token})
    assert first.status_code == 200
    assert second.status_code == 200
    path1, path2 = first.json()["path"], second.json()["path"]
    assert path1 != path2, "each backup call must produce its own file, never overwrite the previous one"
    assert Path(path1).is_file()
    assert Path(path2).is_file()


def test_backup_file_is_a_valid_readable_sqlite_copy_of_metadata(tmp_path: Path):
    root = tmp_path / "input"
    root.mkdir()
    folder = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"
    folder.mkdir()
    from tests.manager.test_scanner import make_pdf
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")

    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    client.post("/scan", headers={"X-CSRF-Token": token})

    resp = client.post("/backup", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    backup_path = Path(resp.json()["path"])
    assert backup_path.is_file()

    conn = sqlite3.connect(backup_path)
    try:
        rows = conn.execute("SELECT folder_name FROM cases").fetchall()
    finally:
        conn.close()
    assert rows and rows[0][0] == "25.000.036.001.015_012345678901_Nguyen_Van_A"


def test_backup_page_ui_clarifies_metadata_only_not_pdf(tmp_path: Path):
    client, _ = _client(tmp_path)
    html = client.get("/backup").text
    assert "PDF" in html
    assert "chỉ mục" in html or "SQLite" in html
    # The button copy itself must not read as "this backs up your PDFs".
    assert "không phải" in html.lower() or "không gồm" in html.lower() or "chỉ mục" in html.lower()


def test_backup_template_uses_post_and_shared_status_messaging():
    html = (TEMPLATES_DIR / "backup.html").read_text(encoding="utf-8")
    assert "method: 'POST'" in html
    assert "X-CSRF-Token" in html
    assert "fetch('/backup/metadata'" not in html
    assert 'class="status-message"' in html
    assert "setStatusMessage(" in html
