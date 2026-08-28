from __future__ import annotations

from pathlib import Path
import json

import pytest
from pypdf import PdfWriter

from app.catalog import load_catalog
from app.models import PipelineError
from app.pdf_inventory import sha256_file
from app.review_repair import (
    apply_repair, create_repair_plan, decide_finding, record_semantic_findings,
    benchmark_fixture, review_history, run_semantic_review, start_review,
)
from app.semantic_reviewer import RenderedPage
from app.semantic_reviewer import OpenAICompatibleSemanticReviewer, SemanticReviewRequest
from app.state import StateRegistry


def _source(path: Path, pages: int = 1) -> None:
    writer = PdfWriter()
    for _ in range(pages): writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.fixture()
def prepared(tmp_path: Path):
    folder = tmp_path / "input" / "P"; folder.mkdir(parents=True)
    source = folder / "a.pdf"; _source(source)
    registry = StateRegistry(tmp_path / "state" / "processing_state.db")
    source_hash = sha256_file(source)
    registry.begin_processing(source_hash=source_hash, source_filename="a.pdf", source_relative_path="P/a.pdf", person_folder="P", page_count=1)
    registry.save_analysis(source_hash, documents=[{
        "source_pages": [1], "type_id": "05", "confidence": 0.99, "document_date": "2020-01-01",
        "date_confidence": 0.99, "title_short": "Quyết định", "segmentation_flags": [],
        "classification_status": "AUTO", "classification_reasons": [], "classification_kind": "TAXONOMY",
        "subtype": None, "date_precision": None,
    }], taxonomy_version="test", analysis_schema_version="1")
    yield registry, folder, tmp_path / "output" / "P", tmp_path / "review" / "P", source_hash
    registry.close()


def test_review_does_not_mutate_and_keep_existing_is_not_planned(prepared):
    registry, folder, output, review, _ = prepared
    before = registry.logical_documents_for_person("P")[0].as_dict()
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    row = registry.logical_documents_for_person("P")[0]
    finding = record_semantic_findings(registry, session.session_id, [{
        "source_hash": row.source_hash, "source_pages": [1], "finding_type": "LOW_CONFIDENCE",
        "existing_result": {"logical_document_id": row.logical_document_id}, "proposed_result": {},
            "reason": "human keeps existing", "confidence": 1, "evidence": {"source": "test"},
    }])[0]
    decide_finding(registry, finding.finding_id, decision="KEEP_EXISTING", reviewer="operator")
    plan = create_repair_plan(registry, session.session_id)
    assert plan.changes == []
    assert registry.logical_documents_for_person("P")[0].as_dict() == before


def test_benchmark_fixture_scrubs_identity_and_uses_filename_invariant(prepared):
    registry, folder, output, review, source_hash = prepared
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    row = registry.logical_documents_for_person("P")[0]
    finding = record_semantic_findings(registry, session.session_id, [{
        "source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_FILENAME",
        "existing_result": {"logical_document_id": row.logical_document_id, "source_hash": source_hash,
            "title_short": "Sensitive", "current_target_filename": "private.pdf"}, "proposed_result": {},
        "reason": "fixture", "confidence": 1, "evidence": {"test": "fixture"},
    }])[0]
    decide_finding(registry, finding.finding_id, decision="ACCEPT", reviewer="operator")
    fixture = benchmark_fixture(registry, finding.finding_id)
    assert fixture["expected_result"] == {"naming": "catalog_deterministic"}
    assert "logical_document_id" not in fixture["old_result"]
    assert "source_hash" not in fixture["old_result"]
    assert "current_target_filename" not in fixture["old_result"]


def test_production_derived_fixture_is_anonymized_and_asserts_filename_invariant():
    path = Path(__file__).parent / "fixtures" / "review_repair" / "production_filename_anonymized.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["source"] == "anonymized"
    assert fixture["finding_type"] == "WRONG_FILENAME"
    assert fixture["expected_result"] == {"naming": "catalog_deterministic"}
    assert set(fixture["old_result"]) <= {"type_id", "classification_kind", "classification_status", "resolution_status", "resolved_classification_kind"}


def test_approved_reclassify_creates_revision_and_is_idempotent(prepared):
    registry, folder, output, review, source_hash = prepared
    before_hash = sha256_file(folder / "a.pdf")
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    row = registry.logical_documents_for_person("P")[0]
    semantic = record_semantic_findings(registry, session.session_id, [{
        "source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_CLASSIFICATION",
        "severity": "HIGH", "existing_result": {"logical_document_id": row.logical_document_id},
            "proposed_result": {"type_id": "07"}, "reason": "Đã có bằng chứng semantic", "confidence": 0.98, "evidence": {"source": "test"},
    }])[0]
    decide_finding(registry, semantic.finding_id, decision="ACCEPT", reviewer="reviewer")
    plan = create_repair_plan(registry, session.session_id)
    preview = apply_repair(registry, plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=output, review_dir=review)
    assert preview["status"] == "DRY_RUN"
    applied = apply_repair(registry, plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=output, review_dir=review, dry_run=False)
    assert applied["status"] == "APPLIED"
    assert registry.logical_documents_for_person("P")[0].effective_type_id == "07"
    assert len(review_history(registry, "P")) == 2
    assert sha256_file(folder / "a.pdf") == before_hash
    assert apply_repair(registry, plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=output, review_dir=review, dry_run=False)["status"] == "ALREADY_APPLIED"


def test_apply_fault_during_transaction_rolls_back_and_new_plan_recovers(prepared, monkeypatch):
    registry, folder, output, review, source_hash = prepared
    source_before = sha256_file(folder / "a.pdf")
    before = registry.logical_documents_for_person("P")[0].as_dict()
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    row = registry.logical_documents_for_person("P")[0]
    finding = record_semantic_findings(registry, session.session_id, [{
        "source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_CLASSIFICATION",
        "existing_result": {"logical_document_id": row.logical_document_id}, "proposed_result": {"type_id": "07"},
        "reason": "fault injection", "confidence": 1, "evidence": {"test": "transaction"},
    }])[0]
    decide_finding(registry, finding.finding_id, decision="ACCEPT", reviewer="test")
    plan = create_repair_plan(registry, session.session_id)
    import app.review_repair as repair
    monkeypatch.setattr(repair, "_assign_all", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected DB-stage failure")))
    with pytest.raises(OSError, match="injected DB-stage failure"):
        apply_repair(registry, plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=output, review_dir=review, dry_run=False)
    assert registry.logical_documents_for_person("P")[0].as_dict() == before
    assert sha256_file(folder / "a.pdf") == source_before
    assert repair.get_repair_plan(registry, plan.repair_plan_id).status == "FAILED"

    monkeypatch.undo()
    recovered, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    retry = record_semantic_findings(registry, recovered.session_id, [{
        "source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_CLASSIFICATION",
        "existing_result": {"logical_document_id": row.logical_document_id}, "proposed_result": {"type_id": "07"},
        "reason": "recovery retry", "confidence": 1, "evidence": {"test": "recovery"},
    }])[0]
    decide_finding(registry, retry.finding_id, decision="ACCEPT", reviewer="test")
    recovered_plan = create_repair_plan(registry, recovered.session_id)
    assert apply_repair(registry, recovered_plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=output, review_dir=review, dry_run=False)["status"] == "APPLIED"
    assert sha256_file(folder / "a.pdf") == source_before


def test_stale_plan_is_rejected(prepared):
    registry, folder, output, review, source_hash = prepared
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    row = registry.logical_documents_for_person("P")[0]
    finding = record_semantic_findings(registry, session.session_id, [{
        "source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_CLASSIFICATION",
        "existing_result": {"logical_document_id": row.logical_document_id}, "proposed_result": {"type_id": "07"},
            "reason": "x", "confidence": 1, "evidence": {"source": "test"},
    }])[0]
    decide_finding(registry, finding.finding_id, decision="ACCEPT", reviewer="op")
    plan = create_repair_plan(registry, session.session_id)
    registry._conn.execute("INSERT INTO case_revisions(person_folder,revision,parent_revision,kind,summary,snapshot_json,created_by,created_at) VALUES ('P',2,1,'TEST','other','{}','test','now')")
    registry._conn.commit()
    with pytest.raises(PipelineError, match="STALE_REVIEW_BASE"):
        apply_repair(registry, plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=output, review_dir=review)


def test_manual_split_is_targeted_and_preserves_source(tmp_path: Path):
    folder = tmp_path / "input" / "P"; folder.mkdir(parents=True)
    source = folder / "a.pdf"; _source(source, pages=2); source_hash = sha256_file(source)
    with StateRegistry(tmp_path / "state" / "processing_state.db") as registry:
        registry.begin_processing(source_hash=source_hash, source_filename="a.pdf", source_relative_path="P/a.pdf", person_folder="P", page_count=2)
        registry.save_analysis(source_hash, documents=[{
            "source_pages": [1, 2], "type_id": "05", "confidence": 0.99, "document_date": "2020-01-01", "date_confidence": 0.99,
            "title_short": "QĐ", "segmentation_flags": [], "classification_status": "AUTO", "classification_reasons": [],
            "classification_kind": "TAXONOMY", "subtype": None, "date_precision": None,
        }], taxonomy_version="t", analysis_schema_version="1")
        session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=tmp_path / "output" / "P", review_dir=tmp_path / "review" / "P")
        row = registry.logical_documents_for_person("P")[0]
        finding = record_semantic_findings(registry, session.session_id, [{
            "source_hash": source_hash, "source_pages": [1, 2], "finding_type": "SHOULD_SPLIT",
            "existing_result": {"logical_document_id": row.logical_document_id},
            "proposed_result": {"documents": [{"source_pages": [1], "type_id": "05"}, {"source_pages": [2], "type_id": "05"}]},
            "reason": "Manual verified boundary", "confidence": 1, "evidence": {"source": "test"},
        }])[0]
        decide_finding(registry, finding.finding_id, decision="ACCEPT", reviewer="op")
        plan = create_repair_plan(registry, session.session_id)
        result = apply_repair(registry, plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=tmp_path / "output" / "P", review_dir=tmp_path / "review" / "P", dry_run=False)
        assert result["status"] == "APPLIED"
        assert [r.source_pages for r in registry.logical_documents_for(source_hash)] == [[1], [2]]
    assert sha256_file(source) == source_hash


class _FakeRenderer:
    def render(self, source, pages):
        assert source.name == "a.pdf"
        return [RenderedPage(page, b"png-test") for page in pages]


class _FakeReviewer:
    reviewer_version = "fake-semantic.v1"
    def __init__(self, findings): self.findings = findings; self.requests = []
    def review(self, request): self.requests.append(request); return self.findings


def test_semantic_review_receives_rendered_scope_and_only_persists_proposal(prepared):
    registry, folder, output, review, source_hash = prepared
    row = registry.logical_documents_for_person("P")[0]
    reviewer = _FakeReviewer([{
        "source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_CLASSIFICATION",
        "existing_result": {"logical_document_id": row.logical_document_id}, "proposed_result": {"type_id": "07"},
        "reason": "Rendered page identifies another form", "confidence": .91, "evidence": {"page": 1, "signal": "heading"},
    }])
    session, findings = run_semantic_review(registry, "P", catalog=load_catalog(), folder=folder, output_dir=output,
        review_dir=review, reviewer=reviewer, renderer=_FakeRenderer(), source_hash=source_hash, pages=(1, 1))
    assert session.review_status == "OPEN" and len(findings) == 1
    assert reviewer.requests[0].rendered_pages[0].png_bytes == b"png-test"
    assert registry.logical_documents_for_person("P")[0].effective_type_id == "05"
    assert findings[0].evidence == {"page": 1, "signal": "heading"}


@pytest.mark.parametrize("bad", [
    {"finding_type": "NOT_A_TYPE"},
    {"finding_type": "WRONG_CLASSIFICATION", "source_pages": [2], "proposed_result": {"type_id": "07"}},
    {"finding_type": "WRONG_CLASSIFICATION", "source_pages": [1], "proposed_result": {"type_id": "999"}},
])
def test_semantic_invalid_output_fails_closed(prepared, bad):
    registry, folder, output, review, source_hash = prepared
    row = registry.logical_documents_for_person("P")[0]
    raw = {"source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_CLASSIFICATION",
           "existing_result": {"logical_document_id": row.logical_document_id}, "proposed_result": {"type_id": "07"},
           "reason": "evidence", "confidence": .9, "evidence": {"page": 1}}
    raw.update(bad)
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    before = len(__import__("app.review_repair", fromlist=["list_findings"]).list_findings(registry, session.session_id))
    with pytest.raises(PipelineError):
        record_semantic_findings(registry, session.session_id, [raw])
    assert len(__import__("app.review_repair", fromlist=["list_findings"]).list_findings(registry, session.session_id)) == before


def test_keep_existing_suppresses_identical_semantic_finding(prepared):
    registry, folder, output, review, source_hash = prepared
    row = registry.logical_documents_for_person("P")[0]
    raw = {"source_hash": source_hash, "source_pages": [1], "finding_type": "LOW_CONFIDENCE",
           "existing_result": {"logical_document_id": row.logical_document_id}, "proposed_result": {},
           "reason": "needs human", "confidence": .6, "evidence": {"signal": "blur"}}
    first, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    finding = record_semantic_findings(registry, first.session_id, [raw], reviewer_version="fake.v1")[0]
    decide_finding(registry, finding.finding_id, decision="KEEP_EXISTING", reviewer="operator")
    second, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    assert record_semantic_findings(registry, second.session_id, [raw], reviewer_version="fake.v1") == []
    third, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    assert len(record_semantic_findings(registry, third.session_id, [raw], reviewer_version="fake.v2")) == 1


def test_semantic_conflicting_proposals_fail_closed(prepared):
    registry, folder, output, review, source_hash = prepared
    row = registry.logical_documents_for_person("P")[0]
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    base = {"source_hash": source_hash, "source_pages": [1], "finding_type": "WRONG_CLASSIFICATION",
        "existing_result": {"logical_document_id": row.logical_document_id}, "reason": "conflict", "confidence": .8, "evidence": {"p": 1}}
    with pytest.raises(PipelineError, match="xung đột"):
        record_semantic_findings(registry, session.session_id, [{**base, "proposed_result": {"type_id": "07"}}, {**base, "proposed_result": {"type_id": "09"}}])


def test_openai_adapter_fails_closed_without_credential_or_on_bad_response(monkeypatch):
    reviewer = OpenAICompatibleSemanticReviewer(endpoint="https://example.invalid/v1/chat/completions", model="vision", api_key_env="TEST_REVIEW_KEY")
    request = SemanticReviewRequest("hash", "a.pdf", [RenderedPage(1, b"png")], [], [], reviewer.reviewer_version)
    monkeypatch.delenv("TEST_REVIEW_KEY", raising=False)
    with pytest.raises(PipelineError, match="SEMANTIC_AI_RUNTIME_PENDING"):
        reviewer.review(request)
    monkeypatch.setenv("TEST_REVIEW_KEY", "test")
    import app.semantic_reviewer as adapter
    monkeypatch.setattr(adapter, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(PipelineError, match="không phản hồi"):
        reviewer.review(request)


def _four_page_case(tmp_path: Path):
    folder = tmp_path / "input" / "P"; folder.mkdir(parents=True)
    source = folder / "a.pdf"; _source(source, pages=4); source_hash = sha256_file(source)
    registry = StateRegistry(tmp_path / "state" / "processing_state.db")
    registry.begin_processing(source_hash=source_hash, source_filename="a.pdf", source_relative_path="P/a.pdf", person_folder="P", page_count=4)
    registry.save_analysis(source_hash, documents=[{
        "source_pages": [page], "type_id": "05", "confidence": .99, "document_date": f"2020-01-0{page}", "date_confidence": .99,
        "title_short": f"Doc {page}", "segmentation_flags": [], "classification_status": "AUTO", "classification_reasons": [],
        "classification_kind": "TAXONOMY", "subtype": None, "date_precision": "DAY",
    } for page in range(1, 5)], taxonomy_version="test", analysis_schema_version="1")
    return registry, folder, tmp_path / "output" / "P", tmp_path / "review" / "P", source_hash


def _apply_one(registry, folder, output, review, source_hash, finding_type, existing, proposed, pages):
    session, _ = start_review(registry, "P", catalog=load_catalog(), output_dir=output, review_dir=review)
    finding = record_semantic_findings(registry, session.session_id, [{
        "source_hash": source_hash, "source_pages": pages, "finding_type": finding_type,
        "existing_result": existing, "proposed_result": proposed, "reason": "structured test evidence",
        "confidence": 1, "evidence": {"test": finding_type},
    }])[0]
    decide_finding(registry, finding.finding_id, decision="ACCEPT", reviewer="test")
    plan = create_repair_plan(registry, session.session_id)
    return apply_repair(registry, plan.repair_plan_id, catalog=load_catalog(), folder=folder, output_dir=output, review_dir=review, dry_run=False)


def test_merge_retires_old_docs_and_only_changes_target_source(tmp_path: Path):
    registry, folder, output, review, source_hash = _four_page_case(tmp_path)
    try:
        rows = sorted(registry.logical_documents_for(source_hash), key=lambda row: row.source_pages); untouched = rows[2].logical_document_id
        result = _apply_one(registry, folder, output, review, source_hash, "SHOULD_MERGE",
            {"document_ids": [rows[0].logical_document_id, rows[1].logical_document_id]},
            {"document_ids": [rows[0].logical_document_id, rows[1].logical_document_id], "source_pages": [1, 2], "type_id": "05"}, [1, 2])
        after = sorted(registry.logical_documents_for(source_hash), key=lambda row: row.source_pages)
        assert result["status"] == "APPLIED" and [r.source_pages for r in after] == [[1, 2], [3], [4]]
        assert untouched in {row.logical_document_id for row in after}
    finally: registry.close()


def test_duplicate_relation_repair_is_executable_and_safe(tmp_path: Path):
    registry, folder, output, review, source_hash = _four_page_case(tmp_path)
    try:
        rows = sorted(registry.logical_documents_for(source_hash), key=lambda row: row.source_pages)
        _apply_one(registry, folder, output, review, source_hash, "MISSED_DUPLICATE",
            {"logical_document_id": rows[1].logical_document_id},
            {"classification_kind": "DUPLICATE", "duplicate_of": rows[0].logical_document_id}, [2])
        fixed = registry.get_logical_document(rows[1].logical_document_id)
        assert fixed.effective_classification_kind == "DUPLICATE" and fixed.duplicate_of == rows[0].logical_document_id
    finally: registry.close()


def test_missing_document_add_and_extra_document_remove_are_executable(tmp_path: Path):
    registry, folder, output, review, source_hash = _four_page_case(tmp_path)
    try:
        rows = sorted(registry.logical_documents_for(source_hash), key=lambda row: row.source_pages)
        # Make page four initially unowned; ADD restores exact source coverage.
        registry._conn.execute("DELETE FROM logical_documents WHERE logical_document_id=?", (rows[3].logical_document_id,)); registry._conn.commit()
        _apply_one(registry, folder, output, review, source_hash, "MISSING_DOCUMENT", {"source_hash": source_hash},
            {"source_hash": source_hash, "source_pages": [4], "type_id": "05", "title_short": "Recovered", "document_date": "2020-01-04"}, [4])
        assert [row.source_pages for row in sorted(registry.logical_documents_for(source_hash), key=lambda row: row.source_pages)] == [[1], [2], [3], [4]]
        # Insert a historical bad overlapping row, then remove it without touching valid docs.
        extra = registry.logical_documents_for(source_hash)[0].as_dict(); extra["logical_document_id"] = "extra-document"; extra["current_target_filename"] = None
        from app.review_repair import _insert_document
        _insert_document(registry._conn, extra); registry._conn.commit()
        _apply_one(registry, folder, output, review, source_hash, "EXTRA_DOCUMENT", {"logical_document_id": "extra-document"}, {}, [1])
        assert registry.get_logical_document("extra-document") is None
    finally: registry.close()


def test_filename_repair_preserves_source_bytes(tmp_path: Path):
    registry, folder, output, review, source_hash = _four_page_case(tmp_path)
    try:
        rows = sorted(registry.logical_documents_for(source_hash), key=lambda row: row.source_pages); original_hash = sha256_file(folder / "a.pdf")
        from app.review_repair import _assign_all
        _assign_all(registry, "P", load_catalog(), output, review, folder)
        original_artifact = registry.get_logical_document(rows[0].logical_document_id).current_target_filename
        (output / original_artifact).replace(output / "wrong.pdf")
        registry._conn.execute("UPDATE logical_documents SET current_target_filename='wrong.pdf' WHERE logical_document_id=?", (rows[0].logical_document_id,)); registry._conn.commit()
        rows = sorted(registry.logical_documents_for(source_hash), key=lambda row: row.source_pages)
        before = {row.logical_document_id: row.as_dict() for row in rows}
        _apply_one(registry, folder, output, review, source_hash, "WRONG_FILENAME",
            {"logical_document_id": rows[0].logical_document_id}, {}, [1])
        renamed = registry.get_logical_document(rows[0].logical_document_id)
        assert renamed.current_target_filename == "05.Quyet_dinh_ket_nap_dang_vien.1.pdf"
        after = {row.logical_document_id: row.as_dict() for row in registry.logical_documents_for(source_hash)}
        assert all(after[key] == value for key, value in before.items() if key != rows[0].logical_document_id)
        assert sha256_file(folder / "a.pdf") == original_hash
    finally: registry.close()


def test_page_order_repair_canonicalizes_legacy_metadata(tmp_path: Path):
    folder = tmp_path / "input" / "P"; folder.mkdir(parents=True)
    source = folder / "a.pdf"; _source(source, pages=2); source_hash = sha256_file(source)
    with StateRegistry(tmp_path / "state" / "processing_state.db") as registry:
        registry.begin_processing(source_hash=source_hash, source_filename="a.pdf", source_relative_path="P/a.pdf", person_folder="P", page_count=2)
        registry.save_analysis(source_hash, documents=[{"source_pages": [1, 2], "type_id": "05", "confidence": .99,
            "document_date": "2020-01-01", "date_confidence": .99, "title_short": "Doc", "segmentation_flags": [],
            "classification_status": "AUTO", "classification_reasons": [], "classification_kind": "TAXONOMY", "subtype": None, "date_precision": "DAY"}], taxonomy_version="t", analysis_schema_version="1")
        row = registry.logical_documents_for(source_hash)[0]
        registry._conn.execute("UPDATE logical_documents SET source_pages=? WHERE logical_document_id=?", ('[2,1]', row.logical_document_id)); registry._conn.commit()
        _apply_one(registry, folder, tmp_path / "output" / "P", tmp_path / "review" / "P", source_hash, "WRONG_PAGE_ORDER",
            {"logical_document_id": row.logical_document_id}, {"source_pages": [1, 2]}, [1, 2])
        assert registry.get_logical_document(row.logical_document_id).source_pages == [1, 2]
    assert sha256_file(source) == source_hash
