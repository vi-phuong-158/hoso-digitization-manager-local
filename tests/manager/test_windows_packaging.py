from __future__ import annotations

import json
import re
from pathlib import Path

from app.manager import entrypoint

SPEC_PATH = Path(__file__).resolve().parents[2] / "HosoManager.spec"
ISS_PATH = Path(__file__).resolve().parents[2] / "installer" / "HosoManager.iss"
ENTRYPOINT_PATH = Path(__file__).resolve().parents[2] / "app" / "manager" / "entrypoint.py"


# ---------------------------------------------------------------------------
# The double-click EXE must never show a console window.
# ---------------------------------------------------------------------------


def test_pyinstaller_spec_builds_windowed_no_console():
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert "console=False" in spec
    assert "console=True" not in spec


def test_installer_shortcuts_launch_the_exe_directly_not_through_a_shell():
    iss = ISS_PATH.read_text(encoding="utf-8")
    shortcut_lines = [line for line in iss.splitlines() if line.strip().startswith("Name:") and "Filename:" in line]
    assert shortcut_lines, "no [Icons] shortcut entries found"
    for line in shortcut_lines:
        filename_match = re.search(r'Filename:\s*"([^"]+)"', line)
        assert filename_match is not None
        target = filename_match.group(1)
        assert target.lower().endswith("hosomanager.exe"), f"shortcut must target the EXE directly, not: {target}"
        for shell in ("cmd.exe", "powershell.exe", ".bat"):
            assert shell not in target.lower()


# ---------------------------------------------------------------------------
# With the console hidden, sys.stdout/stderr can be None under PyInstaller's
# windowed bootloader - anything that logs must not crash, and a startup
# failure must still be discoverable (log file + a plain-language dialog,
# never a raw traceback shown to the operator).
# ---------------------------------------------------------------------------


def test_entrypoint_guards_against_none_stdout_stderr_when_frozen():
    src = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    assert 'getattr(sys, "frozen", False)' in src
    assert "sys.stdout is None" in src
    assert "sys.stderr is None" in src
    assert "os.devnull" in src
    # This guard must run before uvicorn (or anything else that might log) is imported.
    guard_idx = src.index("sys.stdout is None")
    uvicorn_import_idx = src.index("import uvicorn")
    assert guard_idx < uvicorn_import_idx


def test_startup_dialog_never_receives_a_raw_traceback():
    src = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    assert "STARTUP_FAILURE_MESSAGE" in src
    assert '"Không thể khởi động Quản lý hồ sơ Đảng viên. Vui lòng kiểm tra nhật ký lỗi."' in src
    # The dialog call sites (not its `def`) must use the constant message,
    # never format_exc()/str(exc) inline.
    calls = re.findall(r"(?<!def )_show_startup_error_dialog\(([^)]*)\)", src)
    assert len(calls) == 2, f"expected exactly 2 call sites, found {len(calls)}"
    for call in calls:
        assert "exc" not in call, f"dialog must not be passed exception detail directly: {call}"
        assert call.strip() == "STARTUP_FAILURE_MESSAGE"


def test_startup_error_log_never_mentions_document_or_pdf_content():
    src = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    log_fn = re.search(r"def _write_startup_error_log\(.*?\n\n", src, re.S)
    assert log_fn is not None
    body = log_fn.group(0)
    for forbidden in ("pdf", "ocr", "taxonomy_code", "filename", "document"):
        assert forbidden not in body.lower(), f"startup log helper must not reference '{forbidden}'"


def test_startup_failure_is_logged_and_does_not_crash_the_process(tmp_path: Path, monkeypatch):
    root = tmp_path / "app-root"
    root.mkdir()
    not_a_directory = tmp_path / "not-a-directory.txt"
    not_a_directory.write_text("x")
    (root / "config.json").write_text(
        json.dumps({"data_root": str(not_a_directory), "database_path": str(tmp_path / "data" / "manager.db")}),
        encoding="utf-8",
    )

    monkeypatch.setattr(entrypoint, "executable_root", lambda: root)
    monkeypatch.setattr(entrypoint, "acquire_single_instance", lambda r: None)

    entrypoint.main()  # must not raise, even though Settings.validate() fails

    log_path = root / "startup-error.log"
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    assert "Khởi động thất bại" in content
    assert "ValueError" in content
    assert "data_root phải là thư mục" in content


def test_single_instance_second_launch_still_exits_quietly(tmp_path: Path, monkeypatch):
    """Acceptance: single-instance behavior must still PASS after this change -
    a second launch exits without writing an error log (it is not a failure)."""
    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setattr(entrypoint, "executable_root", lambda: root)
    monkeypatch.setattr(entrypoint, "acquire_single_instance", lambda r: False)

    entrypoint.main()

    assert not (root / "startup-error.log").exists()
