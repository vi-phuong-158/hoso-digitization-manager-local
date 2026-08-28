"""Synthetic regression tests for explicit source retirement policy."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import cli
from app.catalog import load_catalog
from app.global_naming import NameableDoc, compute_global_assignment
from app.incremental import DECISION_RETIRED_SOURCE, scan_person_folder
from app.models import MODE_APPLY, MODE_DRY_RUN, PipelineError
from app.pdf_inventory import build_inventory, sha256_file
from app.pipeline import Workspace, process_person_folder
from app.reconcile import reconcile
from app.state import (
    SOURCE_ACTIVE,
    SOURCE_MISSING,
    SOURCE_RETIRED,
    STATUS_PROCESSED,
    STATUS_RETIRED,
    StateRegistry,
)
from state_testkit import add_source


PERSON = "P"


@pytest.fixture()
def env(tmp_path: Path):
    input_root = tmp_path / "input"
    workspace = Workspace(tmp_path)
    registry = StateRegistry(workspace.state_db_path)
    yield tmp_path, input_root, workspace, registry
    registry.close()


def _commit_source(
    input_root: Path,
    registry: StateRegistry,
    filename: str,
    *,
    pages: int = 1,
    type_id: str = "86",
    document_date: str = "2020-01-01",
    title: str = "Synthetic source",
) -> tuple[Path, str]:
    source = add_source(
        input_root, input_root.parent / "unused-analysis", PERSON, filename, n_pages=pages
    )
    # Replace the testkit's analysis dependency with direct state metadata:
    # retirement tests exercise state/lifecycle, not provider cognition.
    source_hash = sha256_file(source)
    registry.begin_processing(
        source_hash=source_hash,
        source_filename=filename,
        source_relative_path=f"{PERSON}/{filename}",
        person_folder=PERSON,
        page_count=pages,
    )
    registry.save_analysis(
        source_hash,
        documents=[
            {
                "source_pages": list(range(1, pages + 1)),
                "type_id": type_id,
                "confidence": 0.97,
                "document_date": document_date,
                "date_confidence": 0.97,
                "title_short": title,
                "segmentation_flags": [],
                "classification_status": "AUTO",
                "classification_reasons": [],
            }
        ],
        taxonomy_version="tx1",
        analysis_schema_version="1.0",
    )
    registry.commit_processed(
        source_hash, logical_document_count=1, manifest_path=f"output/{PERSON}/_manifest.json"
    )
    return source, source_hash


def test_missing_source_detected_without_auto_retire(env):
    tmp_path, input_root, workspace, registry = env
    source, source_hash = _commit_source(input_root, registry, "old.pdf")
    source.unlink()

    lifecycle = registry.source_lifecycle(PERSON, set())
    assert lifecycle[0].lifecycle_status == SOURCE_MISSING
    assert registry.get(source_hash).status == STATUS_PROCESSED
    report = reconcile(
        registry, PERSON, workspace.output / PERSON, workspace.review / PERSON,
        input_root / PERSON,
    )
    assert not report.ok
    assert len(report.missing_sources) == 1
    assert report.retired_sources == []
    assert registry.get(source_hash).retired_at is None


def test_apply_is_blocked_by_unresolved_missing_source(env):
    tmp_path, input_root, workspace, registry = env
    old_source, old_hash = _commit_source(input_root, registry, "old.pdf")
    _commit_source(input_root, registry, "current.pdf")
    old_source.unlink()

    result = process_person_folder(
        input_root / PERSON,
        mode=MODE_APPLY,
        workspace=workspace,
        state_registry=registry,
        write_manifest=False,
    )
    assert result.status == "BLOCKED_MISSING_SOURCE"
    assert registry.get(old_hash).status == STATUS_PROCESSED
    assert not (workspace.output / PERSON).exists()


def test_explicit_retire_preserves_identity_audit_and_is_idempotent(env):
    tmp_path, input_root, workspace, registry = env
    source, source_hash = _commit_source(input_root, registry, "old.pdf", pages=2)
    source.unlink()

    first = registry.retire_source(
        source_hash,
        physical_hashes=set(),
        reason="operator-confirmed archive removal",
        retired_by="alice",
    )
    assert first.outcome == "RETIRED"
    retired = registry.get(source_hash)
    assert retired is not None
    assert retired.status == STATUS_RETIRED
    assert retired.previous_status == STATUS_PROCESSED
    assert retired.source_filename == "old.pdf"
    assert retired.retired_reason == "operator-confirmed archive removal"
    assert retired.retired_by == "alice"
    assert retired.retired_at
    assert retired.audit_provenance == "cli:retire-source"
    assert registry.logical_documents_for(source_hash)

    second = registry.retire_source(source_hash, physical_hashes=set(), reason="different", retired_by="bob")
    assert second.outcome == "ALREADY_RETIRED"
    assert second.retired_at == retired.retired_at
    assert registry.get(source_hash).retired_reason == "operator-confirmed archive removal"
    assert registry._conn.execute("SELECT COUNT(*) FROM source_retirements").fetchone()[0] == 1


def test_retire_rejects_wrong_hash_and_existing_physical_source(env):
    tmp_path, input_root, workspace, registry = env
    source, source_hash = _commit_source(input_root, registry, "old.pdf")

    with pytest.raises(PipelineError, match="không tồn tại"):
        registry.retire_source("wrong-hash", physical_hashes=set())
    with pytest.raises(PipelineError, match="vẫn tồn tại"):
        registry.retire_source(source_hash, physical_hashes={source_hash})
    assert registry.get(source_hash).status == STATUS_PROCESSED


def test_retired_source_is_not_reprocessed_and_is_excluded_from_naming(env):
    tmp_path, input_root, workspace, registry = env
    retired_source, retired_hash = _commit_source(
        input_root, registry, "retired.pdf", document_date="2019-01-01", title="Retired"
    )
    active_a, _ = _commit_source(
        input_root, registry, "active-a.pdf", document_date="2020-01-01", title="Active A"
    )
    active_b, _ = _commit_source(
        input_root, registry, "active-b.pdf", document_date="2021-01-01", title="Active B"
    )
    original_retired_bytes = retired_source.read_bytes()
    retired_source.unlink()
    registry.retire_source(retired_hash, physical_hashes={sha256_file(active_a), sha256_file(active_b)})

    # If the retired bytes reappear, the scan still refuses provider/apply work.
    retired_source.write_bytes(original_retired_bytes)
    inventory = build_inventory(input_root / PERSON)
    retired_decision = next(d for d in scan_person_folder(
        inventory, registry, mode=MODE_DRY_RUN,
        fingerprint=type("FP", (), {"matches_cache": lambda *_: True})(),
    ).decisions if d.source.name == "retired.pdf")
    assert retired_decision.decision == DECISION_RETIRED_SOURCE
    assert not retired_decision.needs_agent and not retired_decision.needs_apply
    retired_source.unlink()

    rows = registry.logical_documents_for_person(PERSON, type_id="86", include_retired=False)
    assignment, reasons = compute_global_assignment(
        load_catalog(), "86", [NameableDoc.from_row(row) for row in rows]
    )
    assert reasons == []
    assert [item.sequence_index for item in assignment] == [1, 2]
    assert all("retired" not in item.target_filename.casefold() for item in assignment)


def test_retired_reconcile_is_clean_but_orphan_is_not(env):
    tmp_path, input_root, workspace, registry = env
    source, source_hash = _commit_source(input_root, registry, "old.pdf")
    source.unlink()
    registry.retire_source(source_hash, physical_hashes=set(), reason="retired")

    clean = reconcile(
        registry, PERSON, workspace.output / PERSON, workspace.review / PERSON,
        input_root / PERSON,
    )
    assert clean.ok
    assert clean.missing_sources == []
    assert len(clean.retired_sources) == 1

    orphan = workspace.output / PERSON / "unowned.pdf"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"%PDF-1.4 orphan")
    dirty = reconcile(
        registry, PERSON, workspace.output / PERSON, workspace.review / PERSON,
        input_root / PERSON,
    )
    assert not dirty.ok
    assert len(dirty.orphans) == 1


def test_retirement_summary_excludes_historical_rows_from_active_counts(env):
    tmp_path, input_root, workspace, registry = env
    source, source_hash = _commit_source(input_root, registry, "old.pdf", pages=3)
    source.unlink()
    registry.retire_source(source_hash, physical_hashes=set())

    summary = registry.summarize_person(PERSON, set())
    assert summary["logical_documents"] == 1
    assert summary["active_logical_documents"] == 0
    assert summary["historical_logical_documents"] == 1
    assert summary["taxonomy"] == 0
    assert summary["active_sources"] == 0
    assert summary["retired_sources"] == 1
    assert summary["retired_pages"] == 3


def test_retirement_transaction_rolls_back_on_audit_failure(env):
    tmp_path, input_root, workspace, registry = env
    source, source_hash = _commit_source(input_root, registry, "old.pdf")
    source.unlink()
    registry._conn.execute(
        """
        CREATE TRIGGER fail_retirement BEFORE INSERT ON source_retirements
        BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END;
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        registry.retire_source(source_hash, physical_hashes=set())
    assert registry.get(source_hash).status == STATUS_PROCESSED
    assert registry.get(source_hash).retired_at is None
    assert registry._conn.execute("SELECT COUNT(*) FROM source_retirements").fetchone()[0] == 0


def test_cli_retire_source_uses_sha_and_is_idempotent(env, capsys):
    tmp_path, input_root, workspace, registry = env
    source, source_hash = _commit_source(input_root, registry, "old.pdf")
    _commit_source(input_root, registry, "current.pdf")
    source.unlink()
    registry.close()

    args = [
        "--root", str(tmp_path), "retire-source", str(input_root / PERSON), source_hash,
        "--reason", "operator confirmed", "--by", "tester", "--json",
    ]
    assert cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "RETIRED"
    assert first["previous_status"] == STATUS_PROCESSED
    assert cli.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["outcome"] == "ALREADY_RETIRED"
