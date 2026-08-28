"""Naming engine: deterministic, chỉ lấy tên từ catalog, thứ tự cũ -> mới."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import UNKNOWN, ClassifiedDocument, DocumentClassification, LogicalDocument, PipelineError
from app.naming import (
    R_ORDER_DUPLICATE_DATE,
    R_ORDER_NO_DATE,
    REVIEW_PREFIX,
    NamingPolicy,
    assign_names,
    auto_filename,
    review_filename,
)

OUT = Path("output/X")
REV = Path("review/X")


def mk(type_id, date=None, date_conf=0.95, pages=(1,), source="a.pdf", status="AUTO", conf=0.97):
    doc = LogicalDocument(source_file=source, source_pages=list(pages), lead_page=pages[0])
    cls = DocumentClassification(
        type_id=type_id, confidence=conf, document_date=date, date_confidence=date_conf
    )
    return ClassifiedDocument(document=doc, classification=cls, classification_status=status)


def test_mot_tai_lieu_cua_loai_khong_co_so_thu_tu(catalog):
    docs = [mk("05", "2018-08-19")]
    assign_names(catalog, docs, OUT, REV)
    assert docs[0].target_file == "05.Quyet_dinh_ket_nap_dang_vien.pdf"
    assert docs[0].sequence_index is None
    assert docs[0].target_dir == "output"
    assert docs[0].final_status == "AUTO"


def test_nhieu_tai_lieu_cung_loai_danh_so_tu_cu_den_moi(catalog):
    docs = [
        mk("87", "2015-11-10", pages=(3,)),
        mk("87", "2015-09-23", pages=(1,)),
        mk("87", "2015-11-03", pages=(2,)),
    ]
    assign_names(catalog, docs, OUT, REV)
    got = {d.document.source_pages[0]: d.target_file for d in docs}
    base = catalog.filename_base("87")
    assert got[1] == f"{base}.1.pdf"  # 2015-09-23
    assert got[2] == f"{base}.2.pdf"  # 2015-11-03
    assert got[3] == f"{base}.3.pdf"  # 2015-11-10
    assert all(d.final_status == "AUTO" for d in docs)


def test_thieu_ngay_thi_ca_nhom_sang_review_khong_danh_so_theo_thu_tu_scan(catalog):
    docs = [mk("87", "2015-09-23", pages=(1,)), mk("87", None, pages=(2,))]
    assign_names(catalog, docs, OUT, REV)
    for d in docs:
        assert d.final_status == "REVIEW"
        assert R_ORDER_NO_DATE in d.final_reasons
        assert d.sequence_index is None
        assert d.target_file.startswith(REVIEW_PREFIX)
        assert d.target_dir == "review"


def test_ngay_trung_nhau_thi_ca_nhom_sang_review(catalog):
    docs = [mk("86", "2015-01-26", pages=(1,)), mk("86", "2015-01-26", pages=(3,))]
    assign_names(catalog, docs, OUT, REV)
    assert all(d.final_status == "REVIEW" for d in docs)
    assert all(R_ORDER_DUPLICATE_DATE in d.final_reasons for d in docs)


def test_ngay_khong_du_tin_cay_thi_khong_duoc_danh_so(catalog):
    docs = [
        mk("87", "2015-09-23", date_conf=0.99, pages=(1,)),
        mk("87", "2016-01-01", date_conf=0.40, pages=(2,)),
    ]
    assign_names(catalog, docs, OUT, REV, NamingPolicy(min_date_confidence=0.80))
    assert all(d.final_status == "REVIEW" for d in docs)


def test_tai_lieu_review_tu_classification_khong_bao_gio_mang_ten_chinh_thuc(catalog):
    docs = [mk("86", "2013-09-10", status="REVIEW", conf=0.62, pages=(17, 18))]
    docs[0].classification_reasons = ["LOW_CONFIDENCE"]
    assign_names(catalog, docs, OUT, REV)
    assert docs[0].target_dir == "review"
    assert docs[0].target_file.startswith(REVIEW_PREFIX)
    assert "LOW_CONFIDENCE" in docs[0].final_reasons


def test_unknown_khong_bao_gio_ra_output(catalog):
    docs = [mk(UNKNOWN, status="REVIEW", conf=0.4)]
    assign_names(catalog, docs, OUT, REV)
    assert docs[0].target_dir == "review"
    assert UNKNOWN in docs[0].target_file


def test_ten_file_chi_sinh_tu_catalog(catalog):
    assert auto_filename(catalog, "01") == "01.Ly_lich_nguoi_xin_vao_dang.pdf"
    assert auto_filename(catalog, "02", 2) == "02.Ly_lich_dang_vien.2.pdf"
    with pytest.raises(PipelineError):
        auto_filename(catalog, "105")


def test_review_filename_truy_vet_duoc_va_bi_gioi_han_do_dai(catalog):
    name = review_filename(catalog, "86", "Bang cap cua HAI.pdf", [17, 18])
    assert name.startswith(REVIEW_PREFIX)
    assert name.endswith(".pdf")
    assert "p17-18" in name
    assert len(name) <= 120


def test_phat_hien_trung_ten_file_dich(catalog):
    # Hai tài liệu bị ép cùng tên -> phải nổ, không được ghi đè âm thầm.
    docs = [mk("05", "2018-01-01", pages=(1,)), mk("05", "2018-01-02", pages=(2,))]
    assign_names(catalog, docs, OUT, REV)
    docs[1].target_file = docs[0].target_file
    docs[1].target_dir = docs[0].target_dir
    from app.naming import _check_paths

    with pytest.raises(PipelineError, match="Trùng tên"):
        _check_paths(docs, OUT, REV, NamingPolicy())


def test_duong_dan_qua_dai_bi_chan_thay_vi_cat_ten_catalog(catalog):
    docs = [mk("86", "2020-01-01")]
    long_dir = Path("C:/" + "d" * 200 + "/output")
    with pytest.raises(PipelineError, match="quá dài"):
        assign_names(catalog, docs, long_dir, long_dir)
