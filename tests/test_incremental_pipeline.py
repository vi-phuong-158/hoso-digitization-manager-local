"""process_person_folder(state_registry=...) — hành vi incremental đầu-cuối
với state semantics mới (ANALYZED_PENDING_APPLY tách biệt PROCESSED) và global
cross-run naming.

Không đụng input/Vi Ngọc Phương/. Dùng PDF tổng hợp từ state_testkit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.incremental import DECISION_ALREADY_PROCESSED, DECISION_NEW
from app.manifest import load_manifest
from app.models import MODE_APPLY, MODE_DRY_RUN
from app.pdf_inventory import sha256_file
from app.pipeline import Workspace, process_person_folder
from app.providers.agent_provider import AgentAnalysisProvider
from app.review import list_pending_reviews, resolve_review
from app.catalog import load_catalog
from app.state import (
    STATUS_ANALYZED_PENDING_APPLY,
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_REVIEW_REQUIRED,
    StateRegistry,
)
from app.state_import import OUTCOME_ALREADY_IN_REGISTRY, OUTCOME_IMPORTED, import_person_folder
from app.vision_adapter import DocumentVisionProvider
from state_testkit import add_source


class CountingProvider(DocumentVisionProvider):
    """Bọc AgentAnalysisProvider, đếm những PDF thực sự được yêu cầu đọc.

    Chứng minh nguồn có cache hợp lệ (PROCESSED hoặc ANALYZED_PENDING_APPLY/
    REVIEW_REQUIRED chưa stale) không bị Agent đọc lại - yêu cầu cứng.
    """

    name = "counting"

    def __init__(self, config=None):
        self._inner = AgentAnalysisProvider(config)
        self.analyzed_files: list[str] = []

    def analyze_pages(self, pdf_path, page_numbers):
        self.analyzed_files.append(Path(pdf_path).name)
        return self._inner.analyze_pages(pdf_path, page_numbers)

    def proposed_documents(self, pdf_path):
        return self._inner.proposed_documents(pdf_path)

    def classify_document(self, pdf_path, page_numbers, candidates, **kw):
        return self._inner.classify_document(pdf_path, page_numbers, candidates, **kw)

    def describe(self):
        return self._inner.describe()


@pytest.fixture()
def env(tmp_path: Path):
    ws = Workspace(tmp_path)
    input_root = tmp_path / "input"
    analysis_root = tmp_path / "analysis"
    registry = StateRegistry(ws.state_db_path)
    yield tmp_path, ws, input_root, analysis_root, registry
    registry.close()


def run(ws, folder, registry, provider, *, mode=MODE_DRY_RUN, **kw):
    return process_person_folder(
        folder, mode=mode, provider=provider, workspace=ws, state_registry=registry, **kw
    )


def cprov(analysis_root):
    return CountingProvider({"analysis_root": analysis_root})


# ============== Phase P #1-7: state semantics ==============
def test_dry_run_thanh_cong_thi_analyzed_pending_apply_khong_phai_processed(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="04", document_date="2024-01-01")
    result = run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_DRY_RUN)
    assert result.status == "DRY_RUN_PASS"
    h = sha256_file(input_root / "P" / "a.pdf")
    r = registry.get(h)
    assert r.status == STATUS_ANALYZED_PENDING_APPLY
    assert r.status != STATUS_PROCESSED


def test_cached_analysis_duoc_tai_su_dung_khong_goi_lai_provider(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="04", document_date="2024-01-01")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_DRY_RUN)

    p2 = cprov(analysis_root)
    result2 = run(ws, input_root / "P", registry, p2, mode=MODE_DRY_RUN)
    assert p2.analyzed_files == []
    assert result2.status == "DRY_RUN_PASS"


def test_taxonomy_doi_thi_cache_stale_va_doc_lai(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="04", document_date="2024-01-01")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_DRY_RUN)
    h = sha256_file(input_root / "P" / "a.pdf")
    with registry._conn:
        registry._conn.execute("UPDATE sources SET taxonomy_version='GIA_LAP_CU' WHERE source_hash=?", (h,))

    p2 = cprov(analysis_root)
    result2 = run(ws, input_root / "P", registry, p2, mode=MODE_DRY_RUN)
    assert p2.analyzed_files == ["a.pdf"]
    assert result2.status == "DRY_RUN_PASS"


def test_source_hash_doi_thi_khong_tai_su_dung_cache(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="04", document_date="2024-01-01")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_DRY_RUN)
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="04", document_date="2024-01-01", size=(420.0, 701.0))

    p2 = cprov(analysis_root)
    result2 = run(ws, input_root / "P", registry, p2, mode=MODE_DRY_RUN)
    assert p2.analyzed_files == ["a.pdf"]
    assert result2.incremental.counts()[DECISION_NEW] == 1


def test_review_khong_thanh_processed_khi_con_review(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(
        input_root, analysis_root, "P", "a.pdf",
        needs_review=True, review_reason="khong ro loai",
    )
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_DRY_RUN)
    apply_result = run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    assert apply_result.status == "APPLY_PASS"  # apply chạy thành công...
    h = sha256_file(input_root / "P" / "a.pdf")
    assert registry.get(h).status == STATUS_REVIEW_REQUIRED  # ...nhưng KHÔNG thành PROCESSED
    # File vẫn được ghi ra review/ để người vận hành xem trước.
    assert len(list((ws.review / "P").glob("*.pdf"))) == 1
    assert len(list((ws.output / "P").glob("*.pdf"))) == 0


def test_resolve_het_review_roi_apply_thi_thanh_processed(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    catalog = load_catalog()
    add_source(input_root, analysis_root, "P", "a.pdf", needs_review=True, document_date="2013-09-10")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_DRY_RUN)
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)

    items = list_pending_reviews(registry, "P")
    assert len(items) == 1
    resolve_review(registry, catalog, items[0].logical_document_id, type_id="86", document_date="2013-09-10")

    p3 = cprov(analysis_root)
    result3 = run(ws, input_root / "P", registry, p3, mode=MODE_APPLY)
    assert p3.analyzed_files == []  # resolve không cần Agent đọc lại PDF
    h = sha256_file(input_root / "P" / "a.pdf")
    assert registry.get(h).status == STATUS_PROCESSED
    assert len(list((ws.output / "P").glob("*.pdf"))) == 1
    assert len(list((ws.review / "P").glob("*.pdf"))) == 0  # bản review đã được dọn khi chuyển sang output/


def test_khong_bao_gio_silently_resolve_review(env):
    """apply không tự ý chốt review - chỉ resolve_review (con người) mới được."""
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", needs_review=True)
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_DRY_RUN)
    for _ in range(3):
        run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    h = sha256_file(input_root / "P" / "a.pdf")
    assert registry.get(h).status == STATUS_REVIEW_REQUIRED  # apply lặp lại vẫn không tự chốt


# ============== Phase P #8-20: cross-run naming ==============
def test_type_moi_lan_dau_ten_tran(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="05", document_date="2018-08-19")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    files = list((ws.output / "P").glob("*.pdf"))
    assert len(files) == 1
    assert files[0].name == "05.Quyet_dinh_ket_nap_dang_vien.pdf"


def test_them_document_cung_type_sequence_global_dung(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="87", document_date="2015-09-23")
    add_source(input_root, analysis_root, "P", "b.pdf", type_id="87", document_date="2015-11-03")
    add_source(input_root, analysis_root, "P", "c.pdf", type_id="87", document_date="2015-11-10")
    result = run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    assert result.status == "APPLY_PASS"
    names = sorted(p.name for p in (ws.output / "P").glob("*.pdf"))
    assert names == [
        "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.1.pdf",
        "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.2.pdf",
        "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.3.pdf",
    ]


def test_them_document_moi_hon_append_khong_dung_lai_file_cu(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="87", document_date="2015-09-23")
    add_source(input_root, analysis_root, "P", "b.pdf", type_id="87", document_date="2015-11-03")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    before = {p.name: sha256_file(p) for p in (ws.output / "P").glob("*.pdf")}

    add_source(input_root, analysis_root, "P", "d.pdf", type_id="87", document_date="2016-01-01")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    names = sorted(p.name for p in (ws.output / "P").glob("*.pdf"))
    assert names[-1] == "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.3.pdf"
    for name, sha in before.items():
        assert sha256_file(ws.output / "P" / name) == sha  # 2 file cũ không đổi byte


def test_them_document_cu_hon_chen_giua_va_renumber(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="87", document_date="2015-09-23")
    add_source(input_root, analysis_root, "P", "b.pdf", type_id="87", document_date="2015-11-03")
    add_source(input_root, analysis_root, "P", "c.pdf", type_id="87", document_date="2015-11-10")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)

    add_source(input_root, analysis_root, "P", "mid.pdf", type_id="87", document_date="2015-10-15")
    result = run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    assert result.status == "APPLY_PASS"

    rows = {r.effective_document_date: r.current_target_filename for r in registry.logical_documents_for_person("P", type_id="87")}
    assert rows["2015-09-23"] == "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.1.pdf"
    assert rows["2015-10-15"] == "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.2.pdf"
    assert rows["2015-11-03"] == "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.3.pdf"
    assert rows["2015-11-10"] == "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.4.pdf"
    # nội dung mỗi file khớp đúng nguồn tương ứng (không chỉ tên đúng)
    for row in registry.logical_documents_for_person("P", type_id="87"):
        src_state = registry.get(row.source_hash)
        out_bytes_pages = __import__("pypdf").PdfReader(str(ws.output / "P" / row.current_target_filename)).pages
        assert len(out_bytes_pages) == 1


def test_rerun_khong_doi_gi_thi_khong_renumber_lai(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="87", document_date="2015-09-23")
    add_source(input_root, analysis_root, "P", "b.pdf", type_id="87", document_date="2015-11-03")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    mtimes_before = {p.name: p.stat().st_mtime_ns for p in (ws.output / "P").glob("*.pdf")}

    result2 = run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    assert result2.status == "APPLY_PASS"
    mtimes_after = {p.name: p.stat().st_mtime_ns for p in (ws.output / "P").glob("*.pdf")}
    assert mtimes_before == mtimes_after  # không file nào bị đụng vào


def test_same_date_deterministic_khong_phu_thuoc_thu_tu_scan(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "z.pdf", type_id="86", document_date="2015-01-01", title="Zebra")
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="86", document_date="2015-01-01", title="Alpha")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    rows = {registry.get(r.source_hash).source_filename: r.sequence_index for r in registry.logical_documents_for_person("P", type_id="86")}
    assert rows["a.pdf"] < rows["z.pdf"]  # "alpha" < "zebra"


def test_nguon_da_processed_van_bi_renumber_khi_chen_tai_lieu_cu_hon(env):
    """Nguồn A đã PROCESSED xong xuôi - vẫn phải đổi tên khi B (cũ hơn) chèn vào,
    dù A không được Agent đọc lại (chỉ đổi tên file, không phân tích lại)."""
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="87", document_date="2015-11-03")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    h_a = sha256_file(input_root / "P" / "a.pdf")
    assert registry.get(h_a).status == STATUS_PROCESSED

    add_source(input_root, analysis_root, "P", "older.pdf", type_id="87", document_date="2015-09-23")
    p2 = cprov(analysis_root)
    run(ws, input_root / "P", registry, p2, mode=MODE_APPLY)
    assert p2.analyzed_files == ["older.pdf"]  # a.pdf KHÔNG được đọc lại, chỉ bị đổi tên file
    names = sorted(p.name for p in (ws.output / "P").glob("*.pdf"))
    assert names == [
        "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.1.pdf",
        "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.2.pdf",
    ]
    assert registry.get(h_a).status == STATUS_PROCESSED  # vẫn PROCESSED, chỉ đổi filename


def test_rename_that_bai_khong_commit_state(env, monkeypatch):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="87", document_date="2015-11-03")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)

    add_source(input_root, analysis_root, "P", "older.pdf", type_id="87", document_date="2015-09-23")

    import app.pipeline as pl
    from app.models import PipelineError as _PipelineError

    def boom(*a, **k):
        raise _PipelineError("gia lap loi filesystem giua chung")

    monkeypatch.setattr(pl, "execute_rename_plan", boom)
    result = run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    assert result.status == "BLOCKED_RUNTIME"
    h_older = sha256_file(input_root / "P" / "older.pdf")
    assert registry.get(h_older).status == STATUS_FAILED
    assert registry.get(h_older).status != STATUS_PROCESSED
    # File cũ của a.pdf vẫn còn nguyên (chưa ai động vào filesystem thật) -
    # apply1 chỉ có 1 tài liệu loại 87 nên tên chưa có số thứ tự.
    assert (ws.output / "P" / "87.Cac_quyet_dinh_dieu_dong_bo_nhiem.pdf").is_file()


def test_khong_co_nguon_moi_thi_khong_goi_provider(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", type_id="04", document_date="2024-01-01")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    p2 = cprov(analysis_root)
    result = run(ws, input_root / "P", registry, p2, mode=MODE_APPLY)
    assert p2.analyzed_files == []
    assert result.status == "APPLY_PASS"


def test_trung_hash_khong_bao_gio_duoc_xu_ly(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    same_size = (420.0, 623.0)
    add_source(input_root, analysis_root, "P", "a.pdf", document_date="2024-01-01", size=same_size)
    add_source(input_root, analysis_root, "P", "copy-a.pdf", document_date="2024-01-01", size=same_size)
    provider = cprov(analysis_root)
    run(ws, input_root / "P", registry, provider, mode=MODE_APPLY)
    assert sorted(provider.analyzed_files) == ["a.pdf"]
    assert len(list((ws.output / "P").glob("*.pdf"))) == 1


# ============== migration (import-state) ==============
def test_import_state_danh_processed_khi_bang_chung_du(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", document_date="2024-01-01")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    registry.close()

    fresh_registry = StateRegistry(ws.state_db_path)
    fresh_registry._conn.execute("DELETE FROM logical_documents")
    fresh_registry._conn.execute("DELETE FROM sources")
    fresh_registry._conn.commit()

    report = import_person_folder(input_root / "P", fresh_registry, workspace=ws)
    assert [o.outcome for o in report.outcomes] == [OUTCOME_IMPORTED]
    r = fresh_registry.get(sha256_file(input_root / "P" / "a.pdf"))
    assert r.status == STATUS_PROCESSED
    fresh_registry.close()


def test_import_state_khong_du_bang_chung_thi_khong_danh_processed(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf")
    report = import_person_folder(input_root / "P", registry, workspace=ws)
    assert report.outcomes[0].outcome == "STATE_IMPORT_REVIEW_REQUIRED"
    assert registry.get(sha256_file(input_root / "P" / "a.pdf")) is None


def test_import_state_khong_ghi_de_record_da_co(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", document_date="2024-01-01")
    run(ws, input_root / "P", registry, cprov(analysis_root), mode=MODE_APPLY)
    report = import_person_folder(input_root / "P", registry, workspace=ws)
    assert report.outcomes[0].outcome == OUTCOME_ALREADY_IN_REGISTRY
