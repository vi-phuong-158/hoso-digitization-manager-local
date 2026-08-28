from __future__ import annotations

import json
from pathlib import Path

from app.manager_core.naming import auto_filename
from app.manager_core.taxonomy import Taxonomy


def test_shared_naming_contract_matches_manager_taxonomy():
    contract = json.loads((Path(__file__).parents[2] / "contracts" / "naming-contract.json").read_text(encoding="utf-8"))
    taxonomy = Taxonomy.load()
    for vector in contract["vectors"]:
        assert auto_filename(taxonomy, vector["type_id"], vector["sequence"]) == vector["filename"]
