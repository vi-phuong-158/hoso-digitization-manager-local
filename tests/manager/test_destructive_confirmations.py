from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.db import Database
from app.manager.main import create_app
from app.manager.scanner import ScanService
from app.manager.status import mark_complete, reopen
from tests.manager.test_scanner import make_pdf

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "manager" / "templates"


def _scanned_case(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    folder = root / "Case"
    folder.mkdir()
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    scanner = ScanService(Settings(data_root=root, database_path=tmp_path / "db.sqlite"), db)
    scanner.scan()
    case_id = db.one("SELECT id FROM cases")["id"]
    return db, case_id


# ---------------------------------------------------------------------------
# Backend: reopen() must not silently destroy the completion audit trail.
# ---------------------------------------------------------------------------


def test_reopen_records_prior_completion_in_history_detail(tmp_path: Path):
    db, case_id = _scanned_case(tmp_path)
    with db.session() as conn:
        mark_complete(conn, case_id, "operator-A")
        completed_row = conn.execute("SELECT completed_at, reviewed_by FROM cases WHERE id=?", (case_id,)).fetchone()
        assert completed_row["completed_at"] is not None
        assert completed_row["reviewed_by"] == "operator-A"

        reopen(conn, case_id)

        after = conn.execute("SELECT completed_at, reviewed_by, effective_status FROM cases WHERE id=?", (case_id,)).fetchone()
        assert after["completed_at"] is None
        assert after["reviewed_by"] is None
        assert after["effective_status"] != "HOAN_THANH"

        history = conn.execute(
            "SELECT detail, created_at FROM case_history WHERE case_id=? AND event_type='REOPENED' ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        assert history is not None, "a REOPENED history row must exist"
        assert history["detail"] is not None
        assert "operator-A" in history["detail"], "who confirmed completion must survive in the audit trail"
        assert completed_row["completed_at"] in history["detail"], "when it was completed must survive in the audit trail"
        assert history["created_at"] is not None  # when it was reopened


def test_reopen_without_prior_completion_leaves_detail_empty(tmp_path: Path):
    db, case_id = _scanned_case(tmp_path)
    with db.session() as conn:
        reopen(conn, case_id)
        history = conn.execute(
            "SELECT detail FROM case_history WHERE case_id=? AND event_type='REOPENED' ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        assert history["detail"] is None


def test_reopen_via_http_persists_audit_trail_and_visible_history(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    folder = root / "Case"
    folder.mkdir()
    make_pdf(folder / "01.Ly_lich_nguoi_xin_vao_dang.pdf")
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")
    client.post("/scan", headers={"X-CSRF-Token": token})
    case_id = client.get("/cases?format=json").json()["items"][0]["id"]

    client.post(f"/cases/{case_id}/complete", json={"reviewed_by": "Nguyen Van Truong"}, headers={"X-CSRF-Token": token})
    reopen_resp = client.post(f"/cases/{case_id}/reopen", headers={"X-CSRF-Token": token})
    assert reopen_resp.status_code == 200

    detail = client.get(f"/cases/{case_id}?format=json").json()
    reopened_events = [h for h in detail["history"] if h["event_type"] == "REOPENED"]
    assert len(reopened_events) == 1
    assert "Nguyen Van Truong" in reopened_events[0]["detail"]

    # The history text must also render on the human-facing page (Vietnamese, no raw enum).
    html = client.get(f"/cases/{case_id}").text
    assert "Nguyen Van Truong" in html
    assert "REOPENED" not in html


# ---------------------------------------------------------------------------
# Frontend contract: destructive actions must route through the shared
# confirmation dialog, never window.alert, never an unconfirmed one-click.
# ---------------------------------------------------------------------------


def test_shared_confirm_dialog_is_defined_once_in_base_template():
    base_html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert 'id="confirm-modal"' in base_html
    assert 'role="alertdialog"' in base_html
    assert "function hosoConfirm" in base_html
    assert "window.hosoConfirm = hosoConfirm" in base_html


def test_case_detail_reopen_button_requires_confirmation():
    html = (TEMPLATES_DIR / "case_detail.html").read_text(encoding="utf-8")
    assert "confirmReopen()" in html
    assert "async function confirmReopen" in html
    assert "await hosoConfirm(" in html
    # The reopen action itself must never fire without going through the confirm gate first.
    reopen_fn = re.search(r"async function confirmReopen\(\)\s*\{(.*?)\n\}", html, re.S)
    assert reopen_fn is not None
    assert "if (!ok) return;" in reopen_fn.group(1)


def test_manual_add_page_delete_and_discard_require_confirmation():
    html = (TEMPLATES_DIR / "manual_add.html").read_text(encoding="utf-8")
    assert html.count("await hosoConfirm(") >= 2, "both page-delete and discard-session must confirm"
    delete_branch = re.search(r"if \(action === 'delete'\) \{(.*?)\n\s*\}", html, re.S)
    assert delete_branch is not None
    assert "await hosoConfirm(" in delete_branch.group(1)
    assert "if (!ok) return;" in delete_branch.group(1)

    discard_handler = re.search(r"\$\('discard'\)\.addEventListener\('click', async \(\) => \{(.*?)\n\}\);", html, re.S)
    assert discard_handler is not None
    assert "await hosoConfirm(" in discard_handler.group(1)


def test_no_window_alert_or_native_confirm_left_in_templates():
    for path in TEMPLATES_DIR.glob("*.html"):
        content = path.read_text(encoding="utf-8")
        assert "window.alert" not in content
        assert re.search(r"\balert\(", content) is None
        # Native confirm() defeats the shared design-system dialog; only the
        # base.html fallback (used if the modal DOM is ever missing) may call it.
        if path.name != "base.html":
            assert re.search(r"\bconfirm\(", content) is None, f"{path.name} calls confirm() directly instead of hosoConfirm()"
