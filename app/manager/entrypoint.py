from __future__ import annotations

import os
import sys

# PyInstaller's windowed/no-console bootloader (console=False) leaves
# sys.stdout/sys.stderr as None on Windows - there is no console to attach
# them to. Any code that logs or prints (uvicorn's own startup logging
# included) would crash with AttributeError the instant it tries to write.
# Redirect to the null device before anything else in this process runs.
if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from app.manager.config import Settings
from app.manager.main import create_app

STARTUP_FAILURE_MESSAGE = "Không thể khởi động Quản lý hồ sơ Đảng viên. Vui lòng kiểm tra nhật ký lỗi."


def executable_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def acquire_single_instance(root: Path):
    """Keep a second double-click from starting a competing localhost server."""
    if sys.platform != "win32":
        return None
    import msvcrt

    lock_path = root / "HosoManager.lock"
    handle = lock_path.open("a+", encoding="ascii")
    handle.seek(0)
    handle.write("0")
    handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    return handle


def _write_startup_error_log(root: Path, exc: BaseException) -> Path:
    """Record the failure so it is debuggable with the console hidden.

    Deliberately just the exception traceback and a timestamp - never PDF
    content, OCR output, or document names, none of which are involved in a
    startup failure (config load, port bind, single-instance lock, app init)
    in the first place.
    """
    log_path = root / "startup-error.log"
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] Khởi động thất bại\n{traceback.format_exc()}\n")
    except OSError:
        pass
    return log_path


def _show_startup_error_dialog(message: str) -> None:
    if sys.platform != "win32":
        print(message, file=sys.stderr)
        return
    import ctypes

    MB_OK = 0x0
    MB_ICONERROR = 0x10
    ctypes.windll.user32.MessageBoxW(None, message, "Hồ sơ Đảng viên", MB_OK | MB_ICONERROR)


def main() -> None:
    root = executable_root()
    settings: Settings | None = None
    instance_lock = None
    try:
        config_path = root / "config.json"
        if config_path.is_file():
            settings = Settings.from_file(config_path)
        else:
            settings = Settings(data_root=root / "done" / "output", database_path=root / "data" / "manager.db", config_path=config_path, open_browser_on_start=True)
            settings.save(config_path)
        settings.validate()
        instance_lock = acquire_single_instance(root)
        if instance_lock is False:
            return
        app = create_app(settings)
    except Exception as exc:  # noqa: BLE001 - last resort: console is hidden, this is the only way the operator finds out
        _write_startup_error_log(root, exc)
        _show_startup_error_dialog(STARTUP_FAILURE_MESSAGE)
        return

    try:
        if settings.open_browser_on_start:
            webbrowser.open(f"http://{settings.host}:{settings.port}/")
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    except Exception as exc:  # noqa: BLE001
        _write_startup_error_log(root, exc)
        _show_startup_error_dialog(STARTUP_FAILURE_MESSAGE)
    finally:
        if instance_lock is not None:
            instance_lock.close()


if __name__ == "__main__":
    main()
