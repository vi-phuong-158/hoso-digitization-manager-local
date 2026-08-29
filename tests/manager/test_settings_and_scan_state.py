from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.main import create_app

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "manager" / "templates"


# ---------------------------------------------------------------------------
# P1-11: saving settings must never report success before the server has
# actually confirmed the write, and a failed save must never trigger a scan.
# ---------------------------------------------------------------------------


def test_settings_save_fails_when_data_root_is_not_a_directory(tmp_path: Path):
    root = tmp_path / "input"
    root.mkdir()
    not_a_directory = tmp_path / "this-is-a-file.txt"
    not_a_directory.write_text("x")
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")

    resp = client.post("/settings", json={"data_root": str(not_a_directory)}, headers={"X-CSRF-Token": token})
    assert resp.status_code >= 400, "an invalid data_root must not be accepted as a successful save"
    assert "data_root" in resp.json().get("detail", "").lower() or "thư mục" in resp.json().get("detail", "")

    # The rejected value must not silently linger in the live, in-memory config -
    # a subsequent scan must still use the last *valid* data_root, not the
    # rejected one.
    live = client.get("/settings", headers={"Accept": "application/json"}).json()
    assert live["data_root"] == str(root.resolve())


def test_settings_save_succeeds_and_persists_new_data_root(tmp_path: Path):
    root = tmp_path / "input"
    root.mkdir()
    new_root = tmp_path / "new-input"
    new_root.mkdir()
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    token = client.get("/").cookies.get("csrf_token")

    resp = client.post("/settings", json={"data_root": str(new_root)}, headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert resp.json()["data_root"] == str(new_root.resolve())

    reread = client.get("/settings", headers={"Accept": "application/json"}).json()
    assert reread["data_root"] == str(new_root.resolve())


def test_settings_template_never_guesses_completion_with_a_timer():
    html = (TEMPLATES_DIR / "settings.html").read_text(encoding="utf-8")
    assert "setTimeout" not in html, "the settings/rescan flow must not use a fixed delay to guess when save finished"
    assert "async function saveSettings" in html
    # The rescan handler must await the save and inspect its result before
    # ever touching /scan.
    rescan_handler = re.search(r"rescanBtn\.addEventListener\('click', async \(\) => \{(.*?)\n\}\);", html, re.S)
    assert rescan_handler is not None
    body = rescan_handler.group(1)
    assert "await saveSettings()" in body
    assert "if (!saved.ok)" in body
    save_call_idx = body.index("await saveSettings()")
    guard_idx = body.index("if (!saved.ok)")
    scan_fetch_idx = body.index("fetch('/scan'")
    assert save_call_idx < guard_idx < scan_fetch_idx, "save must be awaited and checked before /scan is ever called"


# ---------------------------------------------------------------------------
# P1-1 / P1-14: notification states must be distinguishable beyond color,
# and the scan-status contract (folders_seen/files_seen/status) must be
# what every "Quét dữ liệu" surface actually reports.
# ---------------------------------------------------------------------------


def test_status_message_helper_defines_three_distinct_states():
    base_html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert "function setStatusMessage" in base_html
    for kind in ("success", "error", "info"):
        assert f"{kind}:" in base_html, f"missing icon mapping for '{kind}' state"
    assert "role', kind === 'error' ? 'alert' : 'status'" in base_html.replace('"', "'")


def test_no_page_relies_on_a_single_fixed_color_message_class():
    forbidden = {"form-error", "form-message"}
    for path in TEMPLATES_DIR.glob("*.html"):
        content = path.read_text(encoding="utf-8")
        for cls in forbidden:
            assert f'class="{cls}"' not in content, f"{path.name} still uses the old single-color '{cls}' class"


def test_scan_page_reports_real_counts_not_a_nonexistent_field():
    html = (TEMPLATES_DIR / "scan.html").read_text(encoding="utf-8")
    # ScanResult exposes files_seen, not documents_seen - the page must read the real field.
    assert "documents_seen" not in html
    assert "files_seen" in html
