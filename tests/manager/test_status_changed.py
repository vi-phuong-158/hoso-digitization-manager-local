from pathlib import Path

from app.manager.config import Settings
from app.manager.db import Database
from app.manager.scanner import ScanService
from app.manager.status import mark_complete, recompute_case
from tests.manager.test_scanner import make_pdf


def test_changed_after_completion_is_warning_without_auto_downgrade(tmp_path: Path):
    root = tmp_path / "root"; root.mkdir()
    folder = root / "Case"; folder.mkdir()
    pdf = folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf"
    make_pdf(pdf)
    db = Database(tmp_path / "db.sqlite"); db.initialize()
    scanner = ScanService(Settings(data_root=root, database_path=tmp_path / "db.sqlite"), db)
    scanner.scan()
    case_id = db.one("SELECT id FROM cases")["id"]
    with db.session() as conn:
        mark_complete(conn, case_id, "operator")
    pdf.write_bytes(pdf.read_bytes() + b"\n")
    scanner.scan()
    with db.session() as conn:
        result = recompute_case(conn, case_id)
        assert result["effective_status"] == "HOAN_THANH"
        assert conn.execute("SELECT COUNT(*) FROM warnings WHERE case_id=? AND warning_type='CHANGED_AFTER_COMPLETION' AND active=1", (case_id,)).fetchone()[0] == 1
