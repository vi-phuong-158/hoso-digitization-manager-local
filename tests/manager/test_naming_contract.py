from __future__ import annotations

from app.manager.manual_documents import _next_filename
from app.manager.taxonomy import TaxonomyAdapter
from app.manager_core.naming import auto_filename


def test_naming_contract_type_05_type_86_and_suffixes(tmp_path):
    taxonomy = TaxonomyAdapter.load()
    assert auto_filename(taxonomy.catalog, "05") == "05.Quyet_dinh_ket_nap_dang_vien.pdf"
    assert auto_filename(taxonomy.catalog, "86").startswith("86.")
    folder = tmp_path / "Hồ sơ Unicode"; folder.mkdir()
    name, renames = _next_filename(folder, taxonomy, "05")
    assert name.endswith(".pdf") and not renames
    (folder / name).write_bytes(b"x")
    second, renames = _next_filename(folder, taxonomy, "05")
    assert second.endswith(".2.pdf") and len(renames) == 1
    (folder / second).write_bytes(b"x")
    third, _ = _next_filename(folder, taxonomy, "05")
    assert third.endswith(".3.pdf")