from pathlib import Path

from app.manager.config import Settings
from app.manager.db import Database
from app.manager.scanner import ScanService
from app.manager.status import mark_complete, progress_percent, recompute_case, reopen, set_checklist_override
from tests.manager.test_scanner import make_pdf


def setup_case(tmp_path: Path):
    root = tmp_path / "root"; root.mkdir()
    folder = root / "Case"; folder.mkdir()
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    db = Database(tmp_path / "db.sqlite"); db.initialize()
    service = ScanService(Settings(data_root=root, database_path=tmp_path / "db.sqlite"), db)
    service.scan()
    case_id = db.one("SELECT id FROM cases")["id"]
    return db, case_id


def test_auto_status_and_weighted_progress(tmp_path: Path):
    db, case_id = setup_case(tmp_path)
    with db.session() as conn:
        result = recompute_case(conn, case_id)
        assert result["auto_status"] == "CHO_KIEM_TRA"
        assert result["effective_status"] == "CHO_KIEM_TRA"
        assert result["document_count"] == 1
        assert result["progress_percent"] > 0
        assert progress_percent([{"priority": 1, "status": "CO_TAI_LIEU"}, {"priority": 1, "status": "CHUA_XAC_DINH"}]) == 50.0


def test_override_missing_p1_completion_and_reopen(tmp_path: Path):
    db, case_id = setup_case(tmp_path)
    with db.session() as conn:
        result = set_checklist_override(conn, case_id, "02", "CAN_BO_SUNG")
        assert result["auto_status"] == "CAN_BO_SUNG"
        mark_complete(conn, case_id, "operator")
        assert conn.execute("SELECT effective_status FROM cases WHERE id=?", (case_id,)).fetchone()[0] == "HOAN_THANH"
        reopen(conn, case_id)
        assert conn.execute("SELECT manual_status FROM cases WHERE id=?", (case_id,)).fetchone()[0] is None
        assert conn.execute("SELECT effective_status FROM cases WHERE id=?", (case_id,)).fetchone()[0] == "CAN_BO_SUNG"
        assert conn.execute("SELECT COUNT(*) FROM case_history WHERE case_id=?", (case_id,)).fetchone()[0] >= 3


def test_file_wins_over_khong_phat_sinh(tmp_path: Path):
    db, case_id = setup_case(tmp_path)
    with db.session() as conn:
        set_checklist_override(conn, case_id, "02", "KHONG_PHAT_SINH")
        row = next(row for row in recompute_case(conn, case_id)["checklist"] if row["code"] == "01")
        assert row["status"] == "CO_TAI_LIEU"
        row2 = next(row for row in recompute_case(conn, case_id)["checklist"] if row["code"] == "02")
        assert row2["status"] == "KHONG_PHAT_SINH"
