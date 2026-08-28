from pathlib import Path

from pypdf import PdfWriter

from app.manager.config import Settings
from app.manager.db import Database
from app.manager.scanner import ScanService


def make_pdf(path: Path, pages: int = 1, text: str | None = None):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)


def service(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    db = Database(tmp_path / "manager.db")
    db.initialize()
    settings = Settings(data_root=root, database_path=tmp_path / "manager.db")
    return root, db, ScanService(settings, db)


def test_scan_parses_valid_and_malformed_folders_without_mutating_pdf(tmp_path: Path):
    root, db, scanner = service(tmp_path)
    good = root / "25.000.036.001.015_012345678901_Nguyen_Van_A"
    bad = root / "Folder sai"
    good.mkdir(); bad.mkdir()
    good_pdf = good / "01.Ly_lich_nguoi_xin_vao_dang.pdf"
    bad_pdf = bad / "not-a-document.pdf"
    make_pdf(good_pdf); make_pdf(bad_pdf)
    before = good_pdf.read_bytes()
    result = scanner.scan()
    assert result.status == "SUCCESS"
    assert result.folders_seen == 2
    assert good_pdf.read_bytes() == before
    rows = db.all("SELECT * FROM cases ORDER BY id")
    assert rows[0]["person_name_display"] == "Nguyen Van A"
    assert rows[1]["person_name_display"] is None
    assert db.one("SELECT COUNT(*) AS n FROM warnings WHERE warning_type='SAI_TEN_THU_MUC'")["n"] == 1


def test_scan_is_incremental_and_reconciles_deleted_reappeared(tmp_path: Path):
    root, db, scanner = service(tmp_path)
    folder = root / "Case"
    folder.mkdir()
    pdf = folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf"
    make_pdf(pdf)
    scanner.scan()
    first = db.one("SELECT * FROM documents")
    first_hash_time = first["last_hashed_at"]
    second = scanner.scan()
    assert second.status == "SUCCESS"
    unchanged = db.one("SELECT * FROM documents")
    assert unchanged["last_hashed_at"] == first_hash_time
    pdf.unlink()
    scanner.scan()
    assert db.one("SELECT is_present FROM documents")["is_present"] == 0
    pdf.write_bytes(b"not a pdf")
    scanner.scan()
    restored = db.one("SELECT * FROM documents")
    assert restored["is_present"] == 1
    assert restored["parse_status"] == "FILE_KHONG_DOC_DUOC"


def test_duplicate_bytes_create_warning_not_delete(tmp_path: Path):
    root, db, scanner = service(tmp_path)
    folder = root / "Case"
    folder.mkdir()
    first = folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf"
    second = folder / "01.Ly_lich_nguoi_xin_vao_dang.1.pdf"
    make_pdf(first); second.write_bytes(first.read_bytes())
    scanner.scan()
    assert db.one("SELECT COUNT(*) AS n FROM documents WHERE is_present=1")["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM warnings WHERE warning_type='TRUNG_TAI_LIEU'")["n"] == 2
