"""Synthetic end-to-end contracts for the one-command batch orchestrator."""
from __future__ import annotations

import socket
import json
from pathlib import Path

import pytest

from app.batch import (
    BATCH_AUTO_COMPLETE,
    BATCH_AUTO_COMPLETE_WITH_REVIEW,
    PERSON_ALREADY_COMPLETE,
    PERSON_BLOCKED,
    PERSON_COMPLETED,
    PERSON_MISSING_SOURCE,
    PERSON_NEEDS_REVIEW,
    run_batch,
)
from app.pdf_inventory import sha256_file
from app.pipeline import Workspace
from app.providers.agent_provider import AgentAnalysisProvider
from app.state import StateRegistry
from app.vision_adapter import DocumentVisionProvider
from state_testkit import add_source


class CountingProvider(DocumentVisionProvider):
    name = "counting-batch"

    def __init__(self, analysis_root: Path):
        self.inner = AgentAnalysisProvider({"analysis_root": analysis_root})
        self.analyzed_files: list[str] = []

    def analyze_pages(self, pdf_path, page_numbers):
        self.analyzed_files.append(Path(pdf_path).name)
        return self.inner.analyze_pages(pdf_path, page_numbers)

    def proposed_documents(self, pdf_path):
        return self.inner.proposed_documents(pdf_path)

    def classify_document(self, pdf_path, page_numbers, candidates, **kwargs):
        return self.inner.classify_document(pdf_path, page_numbers, candidates, **kwargs)

    def describe(self):
        return {"provider": self.name, "network": "none", "api_key_required": False}


@pytest.fixture()
def env(tmp_path: Path):
    return Workspace(tmp_path), tmp_path / "input", tmp_path / "analysis"


def person(report, name: str):
    return next(item for item in report.people if item.person_folder == name)


def run(ws: Workspace, input_root: Path, analysis_root: Path, **kwargs):
    provider = kwargs.pop("provider", CountingProvider(analysis_root))
    return run_batch(input_root, workspace=ws, provider=provider, **kwargs), provider


def test_new_clean_dossier_auto_apply(env):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "A", "a.pdf", type_id="05", document_date="2020-01-01")

    report, provider = run(ws, input_root, analysis_root)

    assert report.status == BATCH_AUTO_COMPLETE
    item = person(report, "A")
    assert item.status == PERSON_COMPLETED
    assert item.vision_read_sources == 1
    assert provider.analyzed_files == ["a.pdf"]
    assert item.reconciliation_ok
    assert len(list((ws.output / "A").glob("*.pdf"))) == 1
    assert (ws.logs / "batch-report.json").is_file()


def test_processed_is_skipped_and_second_batch_is_idempotent(env):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "A", "a.pdf", type_id="05", document_date="2020-01-01")
    run(ws, input_root, analysis_root)
    before = {path.name: path.stat().st_mtime_ns for path in (ws.output / "A").glob("*.pdf")}

    report, provider = run(ws, input_root, analysis_root)

    assert report.status == BATCH_AUTO_COMPLETE
    assert person(report, "A").status == PERSON_ALREADY_COMPLETE
    assert provider.analyzed_files == []
    assert {path.name: path.stat().st_mtime_ns for path in (ws.output / "A").glob("*.pdf")} == before


def test_only_later_new_source_is_read(env):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "A", "old.pdf", type_id="05", document_date="2020-01-01")
    run(ws, input_root, analysis_root)
    add_source(input_root, analysis_root, "A", "new.pdf", type_id="87", document_date="2021-01-01")

    report, provider = run(ws, input_root, analysis_root)

    assert person(report, "A").status == PERSON_COMPLETED
    assert provider.analyzed_files == ["new.pdf"]
    assert person(report, "A").new_pdfs == 1


def test_review_dossier_never_auto_applies_and_batch_continues(env):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "Review", "review.pdf", needs_review=True, review_reason="uncertain")
    add_source(input_root, analysis_root, "Clean", "clean.pdf", type_id="05", document_date="2020-01-01")

    report, _ = run(ws, input_root, analysis_root)

    assert report.status == BATCH_AUTO_COMPLETE_WITH_REVIEW
    assert person(report, "Review").status == PERSON_NEEDS_REVIEW
    assert person(report, "Clean").status == PERSON_COMPLETED
    assert not (ws.output / "Review").exists()
    assert len(list((ws.output / "Clean").glob("*.pdf"))) == 1


def test_missing_source_goes_to_human_queue(env):
    ws, input_root, analysis_root = env
    source = add_source(input_root, analysis_root, "A", "a.pdf", type_id="05", document_date="2020-01-01")
    run(ws, input_root, analysis_root)
    source.unlink()

    report, provider = run(ws, input_root, analysis_root)

    assert report.status == BATCH_AUTO_COMPLETE_WITH_REVIEW
    assert person(report, "A").status == PERSON_MISSING_SOURCE
    assert person(report, "A").missing == 1
    assert provider.analyzed_files == []


def test_retired_source_is_history_not_active_blocker(env):
    ws, input_root, analysis_root = env
    source = add_source(input_root, analysis_root, "A", "a.pdf", type_id="05", document_date="2020-01-01")
    run(ws, input_root, analysis_root)
    digest = sha256_file(source)
    source.unlink()
    with StateRegistry(ws.state_db_path) as registry:
        registry.retire_source(digest, physical_hashes=set(), reason="synthetic", retired_by="test")

    report, provider = run(ws, input_root, analysis_root)

    assert report.status == BATCH_AUTO_COMPLETE
    assert person(report, "A").status == PERSON_ALREADY_COMPLETE
    assert person(report, "A").retired == 1
    assert provider.analyzed_files == []


def test_apply_failure_isolated_to_one_person(env, monkeypatch):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "A", "a.pdf", type_id="05", document_date="2020-01-01")
    add_source(input_root, analysis_root, "B", "b.pdf", type_id="05", document_date="2020-01-01")
    import app.pipeline as pipeline
    from app.models import PipelineError

    real = pipeline.execute_rename_plan

    def fail_a(output_dir, *args, **kwargs):
        if output_dir.name == "A":
            raise PipelineError("synthetic apply failure")
        return real(output_dir, *args, **kwargs)

    monkeypatch.setattr(pipeline, "execute_rename_plan", fail_a)
    report, _ = run(ws, input_root, analysis_root)

    assert person(report, "A").status == PERSON_BLOCKED
    assert person(report, "B").status == PERSON_COMPLETED
    assert not (ws.output / "A").exists()
    assert len(list((ws.output / "B").glob("*.pdf"))) == 1


def test_person_order_and_no_apply_preview_are_deterministic(env):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "Zulu", "z.pdf", type_id="05", document_date="2020-01-01")
    add_source(input_root, analysis_root, "Alpha", "a.pdf", type_id="05", document_date="2020-01-01")

    report, provider = run(ws, input_root, analysis_root, apply_enabled=False)

    assert [item.person_folder for item in report.people] == ["Alpha", "Zulu"]
    assert all(item.status == "READY" for item in report.people)
    assert sorted(provider.analyzed_files) == ["a.pdf", "z.pdf"]
    assert not (ws.output / "Alpha").exists()


def test_report_uses_effective_review_counts(env):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "A", "a.pdf", needs_review=True, review_reason="uncertain")

    report, _ = run(ws, input_root, analysis_root)

    item = person(report, "A")
    assert item.review == 1
    assert report.counts()["needs_human"] == 1
    assert report.as_dict()["people"][0]["review"] == 1


def test_batch_path_is_offline_when_socket_is_blocked(env, monkeypatch):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "A", "a.pdf", type_id="05", document_date="2020-01-01")

    def blocked(*args, **kwargs):
        raise AssertionError("batch attempted network access")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    report, _ = run(ws, input_root, analysis_root)
    assert report.status == BATCH_AUTO_COMPLETE
    assert report.counts()["provider_network_count"] == 0


def test_agent_batch_plans_missing_analysis_without_poisoning_state(env):
    ws, input_root, analysis_root = env
    # A real runtime agent will fill this JSON after reading only the listed
    # NEW/STALE source.  Batch planning must not mark it FAILED first.
    from state_testkit import make_pdf
    pdf = make_pdf(input_root / "A" / "unread.pdf")
    report = run_batch(
        input_root, workspace=ws,
        provider=AgentAnalysisProvider({"analysis_root": analysis_root}),
    )

    item = person(report, "A")
    assert item.status == PERSON_BLOCKED
    assert item.vision_required_sources == [pdf.name]
    with StateRegistry(ws.state_db_path) as registry:
        assert registry.get(sha256_file(pdf)) is None


def test_cli_batch_run_accepts_one_input_command(env, capsys):
    ws, input_root, analysis_root = env
    add_source(input_root, analysis_root, "A", "a.pdf", type_id="05", document_date="2020-01-01")
    from app.cli import main

    code = main([
        "--root", str(ws.root), "batch-run", str(input_root),
        "--analysis-root", str(analysis_root), "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == BATCH_AUTO_COMPLETE
    assert payload["people"][0]["status"] == PERSON_COMPLETED
