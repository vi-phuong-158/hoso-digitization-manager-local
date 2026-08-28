"""Regression coverage for recovery of source-only legacy registry rows."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.catalog import load_catalog
from app.pdf_inventory import sha256_file
from app.reconcile import reconcile
from app.review import resolve_review
from app.state import (
    RESOLUTION_REVIEW_PENDING,
    STATUS_PROCESSED,
    STATUS_REVIEW_REQUIRED,
    StateRegistry,
    logical_document_id,
)
from app.state_import import (
    OUTCOME_ALREADY_HYDRATED,
    OUTCOME_MISSING_LEGACY_SOURCE,
    OUTCOME_RECOVERED,
    OUTCOME_REVIEW_REQUIRED,
    recover_legacy_person_folder,
)
from app.pipeline import Workspace
from state_testkit import make_pdf


PERSON = "Legacy Person"
SOURCE = "legacy.pdf"


@pytest.fixture()
def env(tmp_path: Path):
    ws = Workspace(tmp_path)
    registry = StateRegistry(ws.state_db_path)
    yield tmp_path, ws, registry
    registry.close()


def _write_analysis(path: Path, *, groups: list[list[int]], review_last: bool = False) -> None:
    pages = []
    docs = []
    for page in range(1, 3):
        pages.append(
            {
                "page_number": page,
                "page_role": "CONTENT",
                "title_guess": f"Legacy page {page}",
                "document_date": "2020-01-01",
                "date_confidence": 0.9,
                "type_candidates": [{"type_id": "04", "confidence": 0.97}],
                "starts_new_document": page in {g[0] for g in groups},
                "continues_previous": False,
                "attach_hint": "NONE",
                "attach_hint_confidence": 0.0,
                "notes": None,
            }
        )
    for index, group in enumerate(groups):
        docs.append(
            {
                "source_pages": group,
                "type_id": "04",
                "confidence": 0.97,
                "document_date": "2020-01-01",
                "date_confidence": 0.9,
                "title_short": f"Legacy {index + 1}",
                "needs_review": review_last and index == len(groups) - 1,
                "review_reason": "legacy review" if review_last and index == len(groups) - 1 else None,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "produced_by": "test-synthetic",
                "person_folder": PERSON,
                "source_file": SOURCE,
                "page_count": 2,
                "pages": pages,
                "documents": docs,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _legacy_setup(tmp_path: Path, ws: Workspace, registry: StateRegistry, *, bad_hash: bool = False):
    source = make_pdf(tmp_path / "input" / PERSON / SOURCE, n_pages=2)
    source_hash = sha256_file(source)
    _write_analysis(tmp_path / "analysis" / PERSON / "legacy.json", groups=[[1], [2]], review_last=True)

    output_dir = ws.output / PERSON
    review_dir = ws.review / PERSON
    make_pdf(output_dir / "04.Legacy.pdf")
    make_pdf(review_dir / "_REVIEW.04.legacy_p2.pdf")
    ledger = {
        "sources": [{"file": SOURCE, "sha256": "0" * 64 if bad_hash else source_hash, "pages": 2}],
        "documents": [
            {
                "source_file": SOURCE,
                "source_pages": [1],
                "type_id": "04",
                "confidence": 0.97,
                "document_date": "2020-01-01",
                "date_confidence": 0.9,
                "title_short": "Legacy 1",
                "classification_status": "AUTO",
                "status": "AUTO",
                "target_dir": "output",
                "target_file": "04.Legacy.pdf",
                "sequence": None,
            },
            {
                "source_file": SOURCE,
                "source_pages": [2],
                "type_id": "04",
                "confidence": 0.97,
                "document_date": "2020-01-01",
                "date_confidence": 0.9,
                "title_short": "Legacy 2",
                "classification_status": "REVIEW",
                "status": "REVIEW",
                "review_reason": ["legacy review"],
                "target_dir": "review",
                "target_file": "_REVIEW.04.legacy_p2.pdf",
                "sequence": None,
            },
        ],
    }
    ledger_path = output_dir / "_manifest.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    registry.import_processed(
        source_hash=source_hash,
        source_filename=SOURCE,
        source_relative_path=f"{PERSON}/{SOURCE}",
        person_folder=PERSON,
        page_count=2,
        logical_document_count=2,
        manifest_path=str(ledger_path),
    )
    return source, source_hash, ledger_path


def _export_without_clock(registry: StateRegistry) -> dict:
    result = registry.export_json()
    result.pop("exported_at", None)
    return result


def test_recover_legacy_rows_preserves_review_artifacts_and_is_idempotent(env):
    tmp_path, ws, registry = env
    _, source_hash, _ = _legacy_setup(tmp_path, ws, registry)

    report = recover_legacy_person_folder(tmp_path / "input" / PERSON, registry, workspace=ws)
    assert [o.outcome for o in report.outcomes] == [OUTCOME_RECOVERED]
    assert report.outcomes[0].restored_logical_documents == 2
    rows = registry.logical_documents_for(source_hash)
    assert [r.logical_document_id for r in rows] == sorted(
        [logical_document_id(source_hash, [1]), logical_document_id(source_hash, [2])]
    )
    assert registry.get(source_hash).status == STATUS_REVIEW_REQUIRED
    assert sum(r.resolution_status == RESOLUTION_REVIEW_PENDING for r in rows) == 1
    assert reconcile(registry, PERSON, ws.output / PERSON, ws.review / PERSON).ok

    before = _export_without_clock(registry)
    rerun = recover_legacy_person_folder(tmp_path / "input" / PERSON, registry, workspace=ws)
    after = _export_without_clock(registry)
    assert [o.outcome for o in rerun.outcomes] == [OUTCOME_ALREADY_HYDRATED]
    assert before == after
    assert len(registry.logical_documents_for(source_hash)) == 2


def test_recovery_refuses_sha_mismatch_without_hydration(env):
    tmp_path, ws, registry = env
    _, source_hash, _ = _legacy_setup(tmp_path, ws, registry, bad_hash=True)
    report = recover_legacy_person_folder(tmp_path / "input" / PERSON, registry, workspace=ws)
    assert report.outcomes[0].outcome == OUTCOME_REVIEW_REQUIRED
    assert registry.logical_documents_for(source_hash) == []
    assert registry.get(source_hash).status == STATUS_PROCESSED


def test_recovery_does_not_hydrate_missing_source(env):
    tmp_path, ws, registry = env
    source, source_hash, _ = _legacy_setup(tmp_path, ws, registry)
    source.unlink()
    report = recover_legacy_person_folder(tmp_path / "input" / PERSON, registry, workspace=ws)
    assert [o.outcome for o in report.outcomes] == [OUTCOME_MISSING_LEGACY_SOURCE]
    assert registry.logical_documents_for(source_hash) == []
    assert registry.get(source_hash).status == STATUS_PROCESSED


def test_recovery_partial_source_restores_only_missing_rows(env):
    tmp_path, ws, registry = env
    _, source_hash, _ = _legacy_setup(tmp_path, ws, registry)
    recover_legacy_person_folder(tmp_path / "input" / PERSON, registry, workspace=ws)
    first = logical_document_id(source_hash, [1])
    second = logical_document_id(source_hash, [2])
    with registry._conn:
        registry._conn.execute("DELETE FROM logical_documents WHERE logical_document_id = ?", (second,))

    report = recover_legacy_person_folder(tmp_path / "input" / PERSON, registry, workspace=ws)
    assert report.outcomes[0].outcome == OUTCOME_RECOVERED
    assert report.outcomes[0].restored_logical_documents == 1
    assert {r.logical_document_id for r in registry.logical_documents_for(source_hash)} == {first, second}


def test_recovery_malformed_ledger_rolls_back_without_partial_rows(env):
    tmp_path, ws, registry = env
    _, source_hash, ledger_path = _legacy_setup(tmp_path, ws, registry)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["documents"][1]["source_pages"] = [1]  # overlap after first valid entry
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    before = _export_without_clock(registry)

    report = recover_legacy_person_folder(tmp_path / "input" / PERSON, registry, workspace=ws)
    after = _export_without_clock(registry)
    assert report.outcomes[0].outcome == OUTCOME_REVIEW_REQUIRED
    assert before == after
    assert registry.logical_documents_for(source_hash) == []


def test_state_summary_uses_effective_kind_and_current_pending_only(env):
    tmp_path, ws, registry = env
    source = make_pdf(tmp_path / "input" / PERSON / "summary.pdf", n_pages=3)
    source_hash = sha256_file(source)
    registry.begin_processing(
        source_hash=source_hash,
        source_filename="summary.pdf",
        source_relative_path=f"{PERSON}/summary.pdf",
        person_folder=PERSON,
        page_count=3,
    )
    registry.save_analysis(
        source_hash,
        documents=[
            {"source_pages": [1], "type_id": "04", "confidence": 0.99, "document_date": None,
             "date_confidence": 0.0, "title_short": "Original", "segmentation_flags": [],
             "classification_status": "AUTO", "classification_reasons": []},
            {"source_pages": [2], "type_id": "UNKNOWN", "confidence": 0.5, "document_date": None,
             "date_confidence": 0.0, "title_short": "Supporting", "segmentation_flags": [],
             "classification_status": "REVIEW", "classification_reasons": ["unknown"]},
            {"source_pages": [3], "type_id": "UNKNOWN", "confidence": 0.5, "document_date": None,
             "date_confidence": 0.0, "title_short": "Duplicate", "segmentation_flags": [],
             "classification_status": "REVIEW", "classification_reasons": ["unknown"]},
        ],
        taxonomy_version="test", analysis_schema_version="1.0",
    )
    original = logical_document_id(source_hash, [1])
    supporting = logical_document_id(source_hash, [2])
    duplicate = logical_document_id(source_hash, [3])
    catalog = load_catalog()
    resolve_review(registry, catalog, supporting, supporting=True)
    resolve_review(registry, catalog, duplicate, duplicate_of=original)
    registry.set_target(supporting, target_filename="historical-review.pdf", target_dir="review", sequence_index=None)

    summary = registry.summarize_person(PERSON)
    assert summary["taxonomy"] == 1
    assert summary["supporting"] == 1
    assert summary["duplicate"] == 1
    assert summary["review_pending"] == 0
    assert summary["historical_review_artifacts"] == 1
