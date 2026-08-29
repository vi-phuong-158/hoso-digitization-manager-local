from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.db import Database
from app.manager.main import create_app
from app.manager.scanner import ScanService
from app.manager.status import mark_complete, recompute_case, set_checklist_override
from tests.manager.fixtures import build_many_cases
from tests.manager.test_scanner import make_pdf


# ---------------------------------------------------------------------------
# P0-2: cases list must not silently truncate at 100/page-size rows.
# ---------------------------------------------------------------------------


def test_pagination_exposes_every_case_including_101_to_150(tmp_path: Path):
    root = tmp_path / "input"
    build_many_cases(root, 150)
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    scan = client.post("/scan", headers={"X-CSRF-Token": token})
    assert scan.status_code == 200
    assert scan.json()["folders_seen"] == 150

    page1 = client.get("/cases?format=json&sort=name&page=1").json()
    assert page1["total"] == 150
    assert page1["page_size"] == 50
    assert page1["total_pages"] == 3
    assert len(page1["items"]) == 50

    page2 = client.get("/cases?format=json&sort=name&page=2").json()
    page3 = client.get("/cases?format=json&sort=name&page=3").json()
    assert len(page2["items"]) == 50
    assert len(page3["items"]) == 50

    all_ids = {row["id"] for row in page1["items"]} | {row["id"] for row in page2["items"]} | {row["id"] for row in page3["items"]}
    assert len(all_ids) == 150, "every case must be reachable across pages, none vanish and none duplicate"

    folder_names = {row["folder_name"] for row in page1["items"] + page2["items"] + page3["items"]}
    assert any(name.endswith("Case_0100") for name in folder_names), "case 101 (0-indexed 100) must be reachable through pagination"
    assert any(name.endswith("Case_0149") for name in folder_names), "case 150 (0-indexed 149) must be reachable through pagination"


def test_pagination_page_size_100_yields_two_pages(tmp_path: Path):
    root = tmp_path / "input"
    build_many_cases(root, 150)
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    client.post("/scan", headers={"X-CSRF-Token": token})

    page1 = client.get("/cases?format=json&page_size=100&page=1").json()
    assert page1["page_size"] == 100
    assert page1["total_pages"] == 2
    assert len(page1["items"]) == 100
    page2 = client.get("/cases?format=json&page_size=100&page=2").json()
    assert len(page2["items"]) == 50


def test_pagination_preserves_filters_across_pages(tmp_path: Path):
    root = tmp_path / "input"
    build_many_cases(root, 120)
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    client.post("/scan", headers={"X-CSRF-Token": token})

    # Every fixture case has one present document -> effective_status CHO_KIEM_TRA.
    filtered_page1 = client.get("/cases?format=json&status=CHO_KIEM_TRA&page=1&page_size=50").json()
    filtered_page2 = client.get("/cases?format=json&status=CHO_KIEM_TRA&page=2&page_size=50").json()
    assert filtered_page1["total"] == 120
    assert all(row["effective_status"] == "CHO_KIEM_TRA" for row in filtered_page1["items"])
    assert all(row["effective_status"] == "CHO_KIEM_TRA" for row in filtered_page2["items"])

    # The rendered HTML page must carry the filter forward into the Next link.
    html = client.get("/cases?status=CHO_KIEM_TRA&page=1").text
    assert "status=CHO_KIEM_TRA" in html
    assert "page=2" in html


def test_out_of_range_page_clamps_instead_of_going_blank(tmp_path: Path):
    root = tmp_path / "input"
    build_many_cases(root, 10)
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    client.post("/scan", headers={"X-CSRF-Token": token})
    result = client.get("/cases?format=json&page=999").json()
    assert result["page"] == 1
    assert len(result["items"]) == 10


# ---------------------------------------------------------------------------
# P0-3: "Còn thiếu" must be one source of truth, identical on list and detail.
# ---------------------------------------------------------------------------


def _scanned_case(tmp_path: Path, name: str = "case"):
    root = tmp_path / name / "root"
    root.mkdir(parents=True)
    folder = root / "Case"
    folder.mkdir()
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    db = Database(tmp_path / name / "db.sqlite")
    db.initialize()
    scanner = ScanService(Settings(data_root=root, database_path=tmp_path / name / "db.sqlite"), db)
    scanner.scan()
    case_id = db.one("SELECT id FROM cases")["id"]
    return db, case_id


def _missing_from_checklist(checklist: list[dict]) -> int:
    """Independent oracle: recount 'missing' straight from the raw checklist rows."""
    return sum(1 for row in checklist if row["status"] == "CAN_BO_SUNG")


def test_missing_count_matches_between_list_and_detail_default_state(tmp_path: Path):
    db, case_id = _scanned_case(tmp_path, "default")
    with db.session() as conn:
        state = recompute_case(conn, case_id)
    # No overrides yet: every unmet item defaults to CHUA_XAC_DINH, not CAN_BO_SUNG.
    assert state["missing_type_count"] == 0
    assert _missing_from_checklist(state["checklist"]) == 0
    row = db.one("SELECT missing_type_count, type_count FROM cases WHERE id=?", (case_id,))
    assert row["missing_type_count"] == 0
    assert row["type_count"] == 1


def test_missing_count_ignores_khong_phat_sinh_overrides(tmp_path: Path):
    db, case_id = _scanned_case(tmp_path, "npsinh")
    with db.session() as conn:
        set_checklist_override(conn, case_id, "02", "KHONG_PHAT_SINH")
        set_checklist_override(conn, case_id, "03", "KHONG_PHAT_SINH")
        state = recompute_case(conn, case_id)
    assert state["missing_type_count"] == 0, "KHONG_PHAT_SINH must never inflate the missing count"
    assert _missing_from_checklist(state["checklist"]) == 0


def test_missing_count_reflects_explicit_can_bo_sung_overrides_consistently(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    folder = root / "Case"
    folder.mkdir()
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    client.post("/scan", headers={"X-CSRF-Token": token})
    case_id = client.get("/cases?format=json").json()["items"][0]["id"]

    for code in ("02", "03", "04"):
        resp = client.post(f"/cases/{case_id}/checklist/{code}", json={"status": "CAN_BO_SUNG"}, headers={"X-CSRF-Token": token})
        assert resp.status_code == 200

    list_row = next(row for row in client.get("/cases?format=json").json()["items"] if row["id"] == case_id)
    detail = client.get(f"/cases/{case_id}?format=json").json()

    assert list_row["missing_type_count"] == 3
    assert detail["case"]["missing_type_count"] == 3
    assert list_row["missing_type_count"] == detail["case"]["missing_type_count"], "cases list and case detail must never disagree"
    assert _missing_from_checklist(detail["checklist"]) == 3, "checklist itself must corroborate the cached count"

    # Resolving one override by adding the actual document must lower the count everywhere.
    folder2 = root / "Case"
    make_pdf(folder2 / "02.Don_xin_vao_dang.pdf")
    client.post("/scan", headers={"X-CSRF-Token": token})
    list_row2 = next(row for row in client.get("/cases?format=json").json()["items"] if row["id"] == case_id)
    detail2 = client.get(f"/cases/{case_id}?format=json").json()
    assert list_row2["missing_type_count"] == 2
    assert detail2["case"]["missing_type_count"] == 2
    assert list_row2["missing_type_count"] == detail2["case"]["missing_type_count"]


def test_missing_count_consistent_after_completion_and_reopen(tmp_path: Path):
    db, case_id = _scanned_case(tmp_path, "complete")
    with db.session() as conn:
        set_checklist_override(conn, case_id, "02", "CAN_BO_SUNG")
        mark_complete(conn, case_id, "operator")
        completed_state = recompute_case(conn, case_id)
    assert completed_state["missing_type_count"] == 1
    assert _missing_from_checklist(completed_state["checklist"]) == 1
    cached = db.one("SELECT missing_type_count FROM cases WHERE id=?", (case_id,))
    assert cached["missing_type_count"] == 1


def test_database_migration_adds_missing_count_columns_to_existing_db(tmp_path: Path):
    """A database created before this fix (no type_count/missing_type_count columns)
    must upgrade in place instead of crashing on first use."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE cases (
          id INTEGER PRIMARY KEY, case_key TEXT UNIQUE NOT NULL, folder_path TEXT UNIQUE NOT NULL,
          folder_name TEXT NOT NULL, m1 TEXT, m2 TEXT, m3 TEXT, m4 TEXT, m5 TEXT, citizen_id TEXT,
          person_name_raw TEXT, person_name_display TEXT, unit_code TEXT,
          auto_status TEXT NOT NULL DEFAULT 'CHUA_XU_LY', manual_status TEXT,
          effective_status TEXT NOT NULL DEFAULT 'CHUA_XU_LY', progress_percent REAL NOT NULL DEFAULT 0,
          document_count INTEGER NOT NULL DEFAULT 0, warning_count INTEGER NOT NULL DEFAULT 0,
          missing_priority1_count INTEGER NOT NULL DEFAULT 0, is_present INTEGER NOT NULL DEFAULT 1,
          first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_scanned_at TEXT,
          completed_at TEXT, reviewed_by TEXT, note TEXT
        )"""
    )
    conn.commit()
    conn.close()

    db = Database(path)
    db.initialize()  # must not raise
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(cases)").fetchall()}
    assert "type_count" in columns
    assert "missing_type_count" in columns
