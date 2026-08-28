from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

import uvicorn

from app.manager.config import Settings
from app.manager.main import create_app


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
def main() -> None:
    root = executable_root()
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
    if settings.open_browser_on_start:
        webbrowser.open(f"http://{settings.host}:{settings.port}/")
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    finally:
        if instance_lock is not None:
            instance_lock.close()


if __name__ == "__main__":
    main()



