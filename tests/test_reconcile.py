"""Phase L — đối chiếu state DB với filesystem thật. Chỉ báo cáo, không tự sửa."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import MODE_APPLY
from app.pipeline import Workspace, process_person_folder
from app.providers.agent_provider import AgentAnalysisProvider
from app.reconcile import reconcile
from app.state import StateRegistry
from state_testkit import add_source


@pytest.fixture()
def env(tmp_path: Path):
    ws = Workspace(tmp_path)
    input_root = tmp_path / "input"
    analysis_root = tmp_path / "analysis"
    registry = StateRegistry(ws.state_db_path)
    yield tmp_path, ws, input_root, analysis_root, registry
    registry.close()


def test_khop_hoan_toan_thi_ok(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", document_date="2024-01-01")
    process_person_folder(
        input_root / "P", mode=MODE_APPLY,
        provider=AgentAnalysisProvider({"analysis_root": analysis_root}), workspace=ws,
        state_registry=registry,
    )
    report = reconcile(registry, "P", ws.output / "P", ws.review / "P")
    assert report.ok
    assert report.missing_on_disk == []
    assert report.orphans == []


def test_phat_hien_thieu_file_tren_dia(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", document_date="2024-01-01")
    process_person_folder(
        input_root / "P", mode=MODE_APPLY,
        provider=AgentAnalysisProvider({"analysis_root": analysis_root}), workspace=ws,
        state_registry=registry,
    )
    for f in (ws.output / "P").glob("*.pdf"):
        f.unlink()

    report = reconcile(registry, "P", ws.output / "P", ws.review / "P")
    assert not report.ok
    assert len(report.missing_on_disk) == 1
    assert report.orphans == []


def test_phat_hien_orphan_tren_dia(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", document_date="2024-01-01")
    process_person_folder(
        input_root / "P", mode=MODE_APPLY,
        provider=AgentAnalysisProvider({"analysis_root": analysis_root}), workspace=ws,
        state_registry=registry,
    )
    (ws.output / "P" / "la_khong_ro_nguon_goc.pdf").write_bytes(b"%PDF-1.4 la")

    report = reconcile(registry, "P", ws.output / "P", ws.review / "P")
    assert not report.ok
    assert report.missing_on_disk == []
    assert len(report.orphans) == 1
    assert "la_khong_ro_nguon_goc.pdf" in report.orphans[0]


def test_reconcile_khong_tu_xoa_hay_tu_sua_gi(env):
    tmp_path, ws, input_root, analysis_root, registry = env
    add_source(input_root, analysis_root, "P", "a.pdf", document_date="2024-01-01")
    process_person_folder(
        input_root / "P", mode=MODE_APPLY,
        provider=AgentAnalysisProvider({"analysis_root": analysis_root}), workspace=ws,
        state_registry=registry,
    )
    orphan = ws.output / "P" / "la.pdf"
    orphan.write_bytes(b"%PDF-1.4 la")
    reconcile(registry, "P", ws.output / "P", ws.review / "P")
    assert orphan.is_file()  # vẫn còn nguyên, không bị dọn tự động
