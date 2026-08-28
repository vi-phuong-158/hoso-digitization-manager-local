"""scan_person_folder: đối chiếu inventory với state registry, KHÔNG mở PDF."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.fingerprint import Fingerprint
from app.incremental import (
    DECISION_ALREADY_PROCESSED,
    DECISION_CACHED_PENDING_APPLY,
    DECISION_CACHED_REVIEW_REQUIRED,
    DECISION_DUPLICATE_SOURCE,
    DECISION_FAILED_PREVIOUSLY,
    DECISION_INTERRUPTED,
    DECISION_NEW,
    DECISION_STALE_ANALYSIS,
    scan_person_folder,
)
from app.models import MODE_APPLY, MODE_DRY_RUN
from app.pdf_inventory import build_inventory, sha256_file
from app.state import StateRegistry
from state_testkit import add_source

FP = Fingerprint(taxonomy_version="tx1", analysis_schema_version="1.0", pipeline_version="v1")
FP_NEW_TAXONOMY = Fingerprint(taxonomy_version="tx2", analysis_schema_version="1.0", pipeline_version="v1")


@pytest.fixture()
def env(tmp_path: Path):
    input_root = tmp_path / "input"
    analysis_root = tmp_path / "analysis"
    reg = StateRegistry(tmp_path / "state" / "processing_state.db")
    yield input_root, analysis_root, reg
    reg.close()


def inv(input_root: Path, person="P"):
    return build_inventory(input_root / person)


def analyzed(reg, h, *, review=False, name="a.pdf", person="P", pages=1, fp=FP):
    reg.begin_processing(source_hash=h, source_filename=name, source_relative_path=f"{person}/{name}", person_folder=person, page_count=pages)
    reg.save_analysis(
        h,
        documents=[{
            "source_pages": [1], "type_id": "04", "confidence": 0.97, "document_date": "2024-01-01",
            "date_confidence": 0.9, "title_short": "X", "segmentation_flags": [],
            "classification_status": "REVIEW" if review else "AUTO", "classification_reasons": [],
        }],
        taxonomy_version=fp.taxonomy_version, analysis_schema_version=fp.analysis_schema_version,
    )


def test_hash_chua_tung_thay_la_new(env):
    input_root, analysis_root, reg = env
    add_source(input_root, analysis_root, "P", "a.pdf")
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    assert [d.decision for d in scan.decisions] == [DECISION_NEW]
    assert scan.needs_agent_sources and scan.needs_agent_sources[0].name == "a.pdf"


def test_processed_thi_skip_khong_process(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    h = sha256_file(src)
    analyzed(reg, h)
    reg.commit_processed(h, logical_document_count=1, manifest_path="x")

    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    d = scan.decisions[0]
    assert d.decision == DECISION_ALREADY_PROCESSED
    assert not d.needs_agent and not d.needs_apply
    assert scan.needs_agent_sources == []


def test_cached_pending_apply_khong_can_agent(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    analyzed(reg, sha256_file(src))
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    d = scan.decisions[0]
    assert d.decision == DECISION_CACHED_PENDING_APPLY
    assert not d.needs_agent


def test_cached_pending_apply_apply_mode_can_ghi_file(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    analyzed(reg, sha256_file(src))
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_APPLY, fingerprint=FP)
    d = scan.decisions[0]
    assert not d.needs_agent  # vẫn không cần Vision
    assert d.needs_apply  # nhưng cần ghi file thật


def test_cached_review_required_mac_dinh_khong_doc_lai(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    analyzed(reg, sha256_file(src), review=True)
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    d = scan.decisions[0]
    assert d.decision == DECISION_CACHED_REVIEW_REQUIRED
    assert not d.needs_agent


def test_cached_review_required_voi_retry_review_doc_lai(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    analyzed(reg, sha256_file(src), review=True)
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP, retry_review=True)
    assert scan.decisions[0].needs_agent


def test_taxonomy_doi_thi_stale(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    analyzed(reg, sha256_file(src), fp=FP)
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP_NEW_TAXONOMY)
    d = scan.decisions[0]
    assert d.decision == DECISION_STALE_ANALYSIS
    assert d.needs_agent


def test_taxonomy_khong_doi_thi_khong_stale(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    analyzed(reg, sha256_file(src), fp=FP)
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    assert scan.decisions[0].decision != DECISION_STALE_ANALYSIS


def test_cung_ten_khac_hash_la_new(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "scan001.pdf")
    analyzed(reg, sha256_file(src), name="scan001.pdf")
    reg.commit_processed(sha256_file(src), logical_document_count=1, manifest_path="x")

    add_source(input_root, analysis_root, "P", "scan001.pdf", size=(420.0, 700.0))  # nội dung khác
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    assert scan.decisions[0].decision == DECISION_NEW


def test_khac_ten_cung_hash_la_duplicate(env):
    input_root, analysis_root, reg = env
    same_size = (420.0, 611.0)
    add_source(input_root, analysis_root, "P", "scan001.pdf", size=same_size)
    add_source(input_root, analysis_root, "P", "copy-scan001.pdf", size=same_size)

    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    by_name = {d.source.name: d for d in scan.decisions}
    # canonical = tên nhỏ nhất theo alphabet -> "copy-..." < "scan..."
    assert by_name["copy-scan001.pdf"].decision == DECISION_NEW
    assert by_name["scan001.pdf"].decision == DECISION_DUPLICATE_SOURCE
    assert not by_name["scan001.pdf"].needs_agent
    assert by_name["scan001.pdf"].duplicate_of_name == "copy-scan001.pdf"
    assert len(scan.needs_agent_sources) == 1


def test_failed_khong_tu_dong_retry(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    h = sha256_file(src)
    reg.begin_processing(source_hash=h, source_filename="a.pdf", source_relative_path="P/a.pdf", person_folder="P", page_count=1)
    reg.mark_failed(h, error="loi")

    for mode in (MODE_DRY_RUN, MODE_APPLY):
        scan = scan_person_folder(inv(input_root), reg, mode=mode, fingerprint=FP)
        assert scan.decisions[0].decision == DECISION_FAILED_PREVIOUSLY
        assert not scan.decisions[0].needs_agent


def test_failed_voi_retry_failed_thi_xu_ly(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    h = sha256_file(src)
    reg.begin_processing(source_hash=h, source_filename="a.pdf", source_relative_path="P/a.pdf", person_folder="P", page_count=1)
    reg.mark_failed(h, error="loi")
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP, retry_failed=True)
    assert scan.decisions[0].needs_agent


def test_processing_con_sot_la_interrupted_khong_tu_retry(env):
    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    h = sha256_file(src)
    reg.begin_processing(source_hash=h, source_filename="a.pdf", source_relative_path="P/a.pdf", person_folder="P", page_count=1)

    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    assert scan.decisions[0].decision == DECISION_INTERRUPTED
    assert not scan.decisions[0].needs_agent

    scan_retry = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP, retry_failed=True)
    assert scan_retry.decisions[0].needs_agent


def test_file_di_chuyen_cung_hash_khong_xu_ly_lai(env):
    input_root, analysis_root, reg = env
    same_size = (420.0, 633.0)
    src = add_source(input_root, analysis_root, "P", "scan1.pdf", size=same_size)
    h = sha256_file(src)
    analyzed(reg, h, name="scan1.pdf")
    reg.commit_processed(h, logical_document_count=1, manifest_path="x")

    src.unlink()
    add_source(input_root, analysis_root, "P", "tai-lieu-cu.pdf", size=same_size)

    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    assert scan.decisions[0].source.name == "tai-lieu-cu.pdf"
    assert scan.decisions[0].decision == DECISION_ALREADY_PROCESSED
    assert scan.needs_agent_sources == []


def test_output_mismatch_khi_state_processed_nhung_thieu_file(env, tmp_path):
    import json

    input_root, analysis_root, reg = env
    src = add_source(input_root, analysis_root, "P", "a.pdf")
    h = sha256_file(src)
    output_dir = tmp_path / "output" / "P"
    review_dir = tmp_path / "review" / "P"
    ledger_path = output_dir / "_manifest.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps({"documents": [{"source_file": "a.pdf", "target_file": "04.X.pdf", "target_dir": "output"}]}),
        encoding="utf-8",
    )
    analyzed(reg, h)
    reg.commit_processed(h, logical_document_count=1, manifest_path=str(ledger_path))

    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP, output_dir=output_dir, review_dir=review_dir)
    d = scan.decisions[0]
    assert d.decision == DECISION_ALREADY_PROCESSED
    assert d.output_mismatch is True
    assert "04.X.pdf" in d.output_mismatch_detail


def test_summary_text_va_counts(env):
    input_root, analysis_root, reg = env
    add_source(input_root, analysis_root, "P", "a.pdf")
    scan = scan_person_folder(inv(input_root), reg, mode=MODE_DRY_RUN, fingerprint=FP)
    assert scan.counts()[DECISION_NEW] == 1
    assert "Mới bổ sung: 1" in scan.summary_text()
