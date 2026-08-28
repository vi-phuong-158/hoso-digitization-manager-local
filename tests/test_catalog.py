"""Catalog là nguồn chân lý: kiểm tra tính toàn vẹn và hàng rào type_id."""
from __future__ import annotations

import json

import pytest

from app.catalog import FILENAME_BASE_RE, Catalog
from app.models import UNKNOWN, PipelineError


def test_catalog_has_104_types(catalog):
    assert len(catalog) == 104
    assert catalog.allowed_type_ids[0] == "01"
    assert catalog.allowed_type_ids[-1] == "104"


def test_filename_base_khong_dau_khong_khoang_trang(catalog):
    for t in catalog.all_types():
        assert FILENAME_BASE_RE.match(t.filename_base), t.filename_base
        assert t.filename_base.startswith(f"{t.id}."), t.filename_base


def test_filename_base_khong_trung(catalog):
    bases = [t.filename_base for t in catalog.all_types()]
    assert len(set(bases)) == len(bases)


def test_type_id_ngoai_catalog_bi_tu_choi(catalog):
    assert catalog.is_valid_classification("86")
    assert catalog.is_valid_classification(UNKNOWN)
    assert not catalog.is_valid_classification("105")
    assert not catalog.is_valid_classification("BANG_CAP")
    with pytest.raises(PipelineError):
        catalog.get("105")


def test_cac_nhom_de_nham_theo_agents_md(catalog):
    assert catalog.are_confusable("70", "86")
    assert catalog.are_confusable("103", "104")
    assert catalog.are_confusable("100", "60")
    assert not catalog.are_confusable("70", "04")
    assert not catalog.are_confusable("86", "86")


def test_catalog_loi_thi_bao_loi_ro_rang(repo_root):
    raw = json.loads((repo_root / "document_types.json").read_text(encoding="utf-8"))
    raw["document_types"][0]["filename_base"] = "01. Ly lich có dấu"
    with pytest.raises(PipelineError, match="filename_base"):
        Catalog(raw, repo_root / "document_types.json")
