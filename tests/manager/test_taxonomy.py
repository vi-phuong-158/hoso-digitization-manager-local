from pathlib import Path

from app.manager.db import Database
from app.manager.taxonomy import TaxonomyAdapter


def test_taxonomy_reuses_official_catalog():
    adapter = TaxonomyAdapter.load()
    assert len(adapter.items) == 104
    assert adapter.path.name == "document_types.json"
    assert adapter.get("01").priority == 1
    assert adapter.get("86").name
    assert adapter.is_valid("UNKNOWN")
    assert not adapter.is_valid("105")


def test_taxonomy_seed_is_idempotent(tmp_path: Path):
    db = Database(tmp_path / "manager.db")
    db.initialize()
    adapter = TaxonomyAdapter.load()
    assert adapter.seed(db) == 104
    assert adapter.seed(db) == 104
    assert len(db.all("SELECT code FROM taxonomy_items")) == 104
