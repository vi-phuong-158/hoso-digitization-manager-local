from __future__ import annotations

import re
from pathlib import Path


def test_manager_source_has_no_pipeline_runtime_imports():
    root = Path(__file__).resolve().parents[2] / "app"
    forbidden = re.compile(r"^\s*(?:from|import)\s+app\.(?:catalog|pipeline|review_repair|semantic_reviewer|state|naming|pdf_inventory)\b", re.MULTILINE)
    for path in (root / "manager").rglob("*.py"):
        assert forbidden.search(path.read_text(encoding="utf-8")) is None, path


def test_gitignore_blocks_operator_data():
    ignored = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8")
    for value in ("/data/", "/done/", "*.db", "*.pdf", "*.jpg", "manager-config.json", "HosoManager.lock"):
        assert value in ignored