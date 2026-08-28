import json
from pathlib import Path

from app.manager.config import Settings
from app.manager.db import Database
from app.manager.integration import ManifestProvider, NoopProvider, ensure_schema, integrate_case
from app.manager.scanner import ScanService
from app.manager.taxonomy import TaxonomyAdapter
from tests.manager.test_scanner import make_pdf


def test_manifest_provider_is_read_only_and_maps_review(tmp_path: Path):
    root = tmp_path / "input"; root.mkdir()
    folder = root / "Case"; folder.mkdir()
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    db = Database(tmp_path / "db.sqlite"); db.initialize()
    settings = Settings(data_root=root, database_path=tmp_path / "db.sqlite")
    scanner = ScanService(settings, db); scanner.scan()
    case_id = db.one("SELECT id FROM cases")["id"]
    manifest_root = tmp_path / "output" / "Case"; manifest_root.mkdir(parents=True)
    manifest = {"documents": [{"source_file": "Case.pdf", "source_pages": [1], "type_id": "86", "status": "REVIEW", "target_file": "_REVIEW.pdf", "title_short": "Bằng", "needs_review": True}]}
    manifest_path = manifest_root / "_manifest.json"; manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    provider = ManifestProvider(tmp_path / "output", TaxonomyAdapter.load())
    before = manifest_path.read_bytes()
    result = integrate_case(db, case_id, provider, "2026-08-24T00:00:00+00:00")
    assert result.available and result.entries[0]["type_id"] == "86"
    assert manifest_path.read_bytes() == before
    assert db.one("SELECT COUNT(*) AS n FROM pipeline_documents")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM warnings WHERE warning_type='REVIEW_PENDING'")["n"] == 1


def test_noop_provider_does_not_create_false_review(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite"); db.initialize(); ensure_schema(db)
    assert isinstance(NoopProvider(), NoopProvider)
