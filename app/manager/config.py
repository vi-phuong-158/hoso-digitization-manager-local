from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    data_root: Path = REPOSITORY_ROOT / "done" / "output"
    database_path: Path = REPOSITORY_ROOT / "data" / "manager.db"
    config_path: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser_on_start: bool = False
    ignore_patterns: list[str] = field(default_factory=lambda: ["Thumbs.db", ".DS_Store", "~$*"])

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "Settings":
        config_path = Path(path) if path else (Path(os.environ["HOSO_MANAGER_CONFIG"]) if os.environ.get("HOSO_MANAGER_CONFIG") else None)
        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8")) if config_path and config_path.is_file() else {}
        base = config_path.parent if config_path else REPOSITORY_ROOT

        def resolve(value: str | None, default: Path) -> Path:
            candidate = Path(value) if value else default
            return candidate if candidate.is_absolute() else (base / candidate).resolve()

        return cls(
            data_root=resolve(raw.get("data_root") or os.environ.get("HOSO_MANAGER_DATA_ROOT"), REPOSITORY_ROOT / "done" / "output"),
            database_path=resolve(raw.get("database_path") or os.environ.get("HOSO_MANAGER_DATABASE_PATH"), REPOSITORY_ROOT / "data" / "manager.db"),
            config_path=config_path,
            host=str(raw.get("host", "127.0.0.1")), port=int(raw.get("port", 8765)),
            open_browser_on_start=bool(raw.get("open_browser_on_start", False)),
            ignore_patterns=list(raw.get("ignore_patterns") or ["Thumbs.db", ".DS_Store", "~$*"]),
        )

    def validate(self) -> None:
        if self.host not in {"127.0.0.1", "localhost"}: raise ValueError("Hồ sơ Manager chỉ được bind localhost")
        if not 1 <= self.port <= 65535: raise ValueError("port phải trong khoảng 1..65535")
        if self.data_root.exists() and not self.data_root.is_dir(): raise ValueError("data_root phải là thư mục")

    def as_dict(self) -> dict[str, Any]:
        return {"data_root": str(self.data_root), "database_path": str(self.database_path), "host": self.host, "port": self.port, "open_browser_on_start": self.open_browser_on_start, "ignore_patterns": self.ignore_patterns}

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.config_path or (REPOSITORY_ROOT / "manager-config.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.config_path = target
        return target