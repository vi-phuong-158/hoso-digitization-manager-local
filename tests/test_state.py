"""StateRegistry: các phép chuyển trạng thái nguyên tử trên SQLite local.

Sáu trạng thái (state.py PERSISTED_STATUSES có 5, NEW là "không có record"):
NEW -> PROCESSING -> ANALYZED_PENDING_APPLY | REVIEW_REQUIRED -> PROCESSED
                   \\-> FAILED
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import PipelineError
from app.state import (
    RESOLUTION_AUTO_RESOLVED,
    RESOLUTION_REVIEW_PENDING,
    RESOLUTION_REVIEW_RESOLVED,
    STATUS_ANALYZED_PENDING_APPLY,
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_PROCESSING,
    STATUS_REVIEW_REQUIRED,
    StateRegistry,
    logical_document_id,
)


@pytest.fixture()
def registry(tmp_path: Path):
    with StateRegistry(tmp_path / "state" / "processing_state.db") as reg:
        yield reg


def begin(reg, h="h1", name="a.pdf", person="P", pages=3):
    reg.begin_processing(
        source_hash=h, source_filename=name, source_relative_path=f"{person}/{name}",
        person_folder=person, page_count=pages,
    )


def doc(pages=(1,), type_id="04", status="AUTO", date="2024-01-01", date_conf=0.9, title="X"):
    return {
        "source_pages": list(pages), "type_id": type_id, "confidence": 0.97,
        "document_date": date, "date_confidence": date_conf, "title_short": title,
        "segmentation_flags": [], "classification_status": status, "classification_reasons": [],
    }


# ---------------- transitions cơ bản ----------------
def test_hash_chua_tung_thay_thi_khong_co_record(registry):
    assert registry.get("khong-ton-tai") is None


def test_begin_processing_tao_record_processing(registry):
    begin(registry)
    r = registry.get("h1")
    assert r.status == STATUS_PROCESSING
    assert r.first_seen_at


def test_state_db_la_file_that_tren_dia(tmp_path: Path, registry):
    assert (tmp_path / "state" / "processing_state.db").is_file()


def test_save_analysis_khong_review_thi_analyzed_pending_apply(registry):
    begin(registry)
    registry.save_analysis(
        "h1", documents=[doc()], taxonomy_version="tx1", analysis_schema_version="1.0"
    )
    r = registry.get("h1")
    assert r.status == STATUS_ANALYZED_PENDING_APPLY
    assert r.taxonomy_version == "tx1"
    assert r.status != STATUS_PROCESSED  # AI đọc xong KHÔNG đồng nghĩa nghiệp vụ xong


def test_save_analysis_co_review_thi_review_required(registry):
    begin(registry)
    registry.save_analysis(
        "h1", documents=[doc(status="AUTO"), doc(pages=(2,), status="REVIEW")],
        taxonomy_version="tx1", analysis_schema_version="1.0",
    )
    assert registry.get("h1").status == STATUS_REVIEW_REQUIRED
    pending = registry.pending_reviews_for_source("h1")
    assert len(pending) == 1
    assert pending[0].resolution_status == RESOLUTION_REVIEW_PENDING


def test_logical_documents_luu_dung_du_lieu(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc(pages=(1, 2), type_id="86")], taxonomy_version="t", analysis_schema_version="1.0")
    rows = registry.logical_documents_for("h1")
    assert len(rows) == 1
    assert rows[0].source_pages == [1, 2]
    assert rows[0].type_id == "86"
    assert rows[0].resolution_status == RESOLUTION_AUTO_RESOLVED


def test_commit_processed_that_bai_neu_con_review_pending(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc(status="REVIEW")], taxonomy_version="t", analysis_schema_version="1.0")
    with pytest.raises(PipelineError, match="REVIEW_PENDING"):
        registry.commit_processed("h1", logical_document_count=1, manifest_path="x")
    assert registry.get("h1").status != STATUS_PROCESSED


def test_commit_processed_thanh_cong_khi_het_review(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc()], taxonomy_version="t", analysis_schema_version="1.0")
    registry.commit_processed("h1", logical_document_count=1, manifest_path="x")
    r = registry.get("h1")
    assert r.status == STATUS_PROCESSED
    assert r.processed_at is not None


def test_commit_processed_tren_processing_thi_bao_loi(registry):
    begin(registry)
    with pytest.raises(PipelineError):
        registry.commit_processed("h1", logical_document_count=1, manifest_path="x")


def test_mark_failed_tu_cac_trang_thai_hop_le(registry):
    begin(registry)
    registry.mark_failed("h1", error="loi ky thuat")
    r = registry.get("h1")
    assert r.status == STATUS_FAILED
    assert r.status != STATUS_PROCESSED


def test_retry_qua_begin_processing_xoa_logical_documents_cu(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc(), doc(pages=(2,))], taxonomy_version="t", analysis_schema_version="1.0")
    assert len(registry.logical_documents_for("h1")) == 2
    begin(registry)  # retry
    assert registry.get("h1").status == STATUS_PROCESSING
    assert registry.logical_documents_for("h1") == []  # phân tích cũ không còn giá trị


def test_processing_con_sot_la_dau_hieu_interrupted(registry):
    begin(registry)
    r = registry.get("h1")
    assert r.status == STATUS_PROCESSING  # lần "chạy sau" đọc lại sẽ suy ra INTERRUPTED


def test_last_error_bi_gioi_han_do_dai(registry):
    begin(registry)
    registry.mark_failed("h1", error="x" * 5000)
    assert len(registry.get("h1").last_error) <= 2000


# ---------------- resolve review ----------------
def test_resolve_review_chuyen_review_resolved(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc(status="REVIEW")], taxonomy_version="t", analysis_schema_version="1.0")
    lid = registry.logical_documents_for("h1")[0].logical_document_id
    registry.resolve_review(lid, resolved_type_id="86", resolved_document_date="2020-01-01", resolved_by="op")
    row = registry.get_logical_document(lid)
    assert row.resolution_status == RESOLUTION_REVIEW_RESOLVED
    assert row.effective_type_id == "86"
    assert row.effective_document_date == "2020-01-01"


def test_resolve_review_hai_lan_bi_tu_choi(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc(status="REVIEW")], taxonomy_version="t", analysis_schema_version="1.0")
    lid = registry.logical_documents_for("h1")[0].logical_document_id
    registry.resolve_review(lid, resolved_type_id="86", resolved_document_date=None, resolved_by="op")
    with pytest.raises(PipelineError):
        registry.resolve_review(lid, resolved_type_id="87", resolved_document_date=None, resolved_by="op")


def test_pending_reviews_giam_dan_khi_resolve(registry):
    begin(registry)
    registry.save_analysis(
        "h1", documents=[doc(status="REVIEW"), doc(pages=(2,), status="REVIEW")],
        taxonomy_version="t", analysis_schema_version="1.0",
    )
    lids = [r.logical_document_id for r in registry.pending_reviews_for_source("h1")]
    assert len(lids) == 2
    registry.resolve_review(lids[0], resolved_type_id="86", resolved_document_date=None, resolved_by="op")
    assert len(registry.pending_reviews_for_source("h1")) == 1


# ---------------- logical_document_id ổn định (Phase K) ----------------
def test_logical_document_id_on_dinh_theo_hash_va_pages():
    assert logical_document_id("h1", [1, 2]) == logical_document_id("h1", [1, 2])
    assert logical_document_id("h1", [1, 2]) != logical_document_id("h1", [1, 3])
    assert logical_document_id("h1", [1, 2]) != logical_document_id("h2", [1, 2])


def test_logical_document_id_khong_doi_sau_khi_set_target(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc()], taxonomy_version="t", analysis_schema_version="1.0")
    lid_before = registry.logical_documents_for("h1")[0].logical_document_id
    registry.set_target(lid_before, target_filename="04.x.1.pdf", target_dir="output", sequence_index=1)
    registry.set_target(lid_before, target_filename="04.x.2.pdf", target_dir="output", sequence_index=2)
    lid_after = registry.logical_documents_for("h1")[0].logical_document_id
    assert lid_before == lid_after


# ---------------- export / import ----------------
def test_export_json_khong_chua_noi_dung_tai_lieu(registry):
    begin(registry)
    registry.save_analysis("h1", documents=[doc()], taxonomy_version="t", analysis_schema_version="1.0")
    registry.commit_processed("h1", logical_document_count=1, manifest_path="x")
    data = registry.export_json()
    assert data["sources"][0]["source_hash"] == "h1"
    assert "source_pages" in data["logical_documents"][0]
    # Không trường nào chứa văn bản/OCR toàn văn.
    for key in data["logical_documents"][0]:
        assert "full_text" not in key and "ocr" not in key.lower()


def test_import_processed_khong_di_qua_processing(registry):
    registry.import_processed(
        source_hash="h9", source_filename="old.pdf", source_relative_path="P/old.pdf",
        person_folder="P", page_count=2, logical_document_count=2, manifest_path="output/P/_manifest.json",
    )
    r = registry.get("h9")
    assert r.status == STATUS_PROCESSED
    assert r.processing_started_at is None


def test_import_processed_khong_ghi_de_record_da_co(registry):
    begin(registry, h="h1")
    registry.save_analysis("h1", documents=[doc()], taxonomy_version="t", analysis_schema_version="1.0")
    registry.commit_processed("h1", logical_document_count=1, manifest_path="a")
    registry.import_processed(
        source_hash="h1", source_filename="a.pdf", source_relative_path="P/a.pdf",
        person_folder="P", page_count=3, logical_document_count=99, manifest_path="KHAC",
    )
    assert registry.get("h1").logical_document_count == 1  # không bị import ghi đè
