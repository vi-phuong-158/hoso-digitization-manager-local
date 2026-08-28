"""Segmentation: một PDF nhiều tài liệu, ghép bìa/mặt sau, bất định -> REVIEW."""
from __future__ import annotations

import pytest

from app.models import PipelineError
from app.segmenter import (
    FLAG_AMBIGUOUS_COVER,
    FLAG_BACKSIDE_MISMATCH,
    FLAG_ORPHAN_ATTACHMENT,
    FLAG_WEAK_BOUNDARY,
    segment_source,
)
from conftest import make_source, obs

A4 = (595.0, 842.0)
LANDSCAPE = (840.0, 597.0)


def pages_of(docs):
    return [d.source_pages for d in docs]


def test_mot_pdf_chua_nhieu_tai_lieu_doc_lap():
    src = make_source("qd.pdf", [A4, A4, A4])
    docs = segment_source(
        src,
        [
            obs(1, title="Quyết định điều động học viên ra trường"),
            obs(2, title="Quyết định kết nạp đảng viên"),
            obs(3, title="Quyết định công nhận đảng viên chính thức"),
        ],
    )
    assert pages_of(docs) == [[1], [2], [3]]


def test_bia_ngay_sau_trang_noi_dung_duoc_ghep_dung():
    src = make_source("bangcap.pdf", [LANDSCAPE, (841.0, 593.0), (824.0, 598.0), (824.0, 598.0)])
    docs = segment_source(
        src,
        [
            obs(1, title="Chứng chỉ tin học ứng dụng trình độ A"),
            obs(2, role="COVER", title="Chứng chỉ tin học ứng dụng", hint="PREVIOUS", hint_conf=0.9),
            obs(3, title="Bằng tốt nghiệp trung cấp chuyên nghiệp"),
            obs(4, role="COVER", title="Bằng tốt nghiệp trung cấp chuyên nghiệp", hint="PREVIOUS", hint_conf=0.9),
        ],
    )
    assert pages_of(docs) == [[1, 2], [3, 4]]


def test_bia_dung_truoc_noi_dung_van_ghep_dung_huong():
    src = make_source("bia_truoc.pdf", [(540.0, 368.0), (540.0, 368.0), (595.0, 842.0)])
    docs = segment_source(
        src,
        [
            obs(1, role="COVER", title="Chứng chỉ ngoại ngữ", hint="NEXT", hint_conf=0.9),
            obs(2, title="Chứng chỉ ngoại ngữ trình độ A1"),
            obs(3, title="Quyết định kết nạp đảng viên"),
        ],
    )
    assert pages_of(docs) == [[1, 2], [3]]


def test_mat_sau_ghep_voi_mat_truoc():
    src = make_source("ks.pdf", [(539.0, 760.0), (539.0, 758.0), (595.0, 842.0)])
    docs = segment_source(
        src,
        [
            obs(1, title="Giấy khai sinh (bản sao)"),
            obs(2, role="BACK_SIDE", hint="PREVIOUS", hint_conf=0.95),
            obs(3, title="Quyết định công nhận đảng viên chính thức"),
        ],
    )
    assert pages_of(docs) == [[1, 2], [3]]
    assert docs[0].segmentation_flags == []


def test_mat_sau_khac_kho_giay_bi_gan_co_review():
    src = make_source("ks.pdf", [(539.0, 760.0), (300.0, 200.0)])
    docs = segment_source(
        src, [obs(1, title="Giấy khai sinh"), obs(2, role="BACK_SIDE", hint="PREVIOUS", hint_conf=0.9)]
    )
    assert pages_of(docs) == [[1, 2]]
    assert FLAG_BACKSIDE_MISMATCH in docs[0].segmentation_flags


def test_bia_khong_ro_thuoc_truoc_hay_sau_thi_sang_review():
    # Cùng khổ giấy, tiêu đề bìa trung tính, model không dám chỉ hướng.
    src = make_source("mo_ho.pdf", [(600.0, 800.0), (600.0, 800.0), (600.0, 800.0)])
    docs = segment_source(
        src,
        [
            obs(1, title="Chứng chỉ nghiệp vụ"),
            obs(2, role="COVER", title="Chứng chỉ nghiệp vụ", hint="UNCERTAIN", hint_conf=0.0),
            obs(3, title="Chứng chỉ nghiệp vụ"),
        ],
    )
    assert pages_of(docs) == [[1], [2], [3]]
    assert FLAG_AMBIGUOUS_COVER in docs[1].segmentation_flags


def test_trang_phu_khong_co_cha_van_duoc_ke_toi():
    src = make_source("orphan.pdf", [(600.0, 800.0), (595.0, 842.0)])
    docs = segment_source(
        src,
        [
            obs(1, role="COVER", title=None, hint="UNCERTAIN"),
            obs(2, title="Quyết định kết nạp đảng viên"),
        ],
    )
    all_pages = sorted(p for d in docs for p in d.source_pages)
    assert all_pages == [1, 2]
    orphan = [d for d in docs if d.source_pages == [1]][0]
    assert orphan.segmentation_flags  # phải có cờ để sang REVIEW


def test_trang_tiep_noi_duoc_gom_vao_tai_lieu_truoc():
    src = make_source("lylich.pdf", [(595.0, 842.0)] * 4)
    docs = segment_source(
        src,
        [
            obs(1, title="Lý lịch đảng viên"),
            obs(2, role="CONTINUATION", continues=True),
            obs(3, role="CONTINUATION", continues=True),
            obs(4, title="Phiếu đảng viên"),
        ],
    )
    assert pages_of(docs) == [[1, 2, 3], [4]]


def test_trang_noi_dung_khong_khang_dinh_mo_dau_bi_gan_co_weak_boundary():
    src = make_source("weak.pdf", [(595.0, 842.0)])
    docs = segment_source(src, [obs(1, title="Văn bản gì đó", starts=False)])
    assert FLAG_WEAK_BOUNDARY in docs[0].segmentation_flags


def test_moi_trang_thuoc_dung_mot_tai_lieu():
    src = make_source("cover.pdf", [(595.0, 842.0)] * 6)
    docs = segment_source(
        src,
        [
            obs(1, title="A"),
            obs(2, role="CONTINUATION", continues=True),
            obs(3, title="B"),
            obs(4, role="COVER", title="B", hint="PREVIOUS", hint_conf=0.9),
            obs(5, title="C"),
            obs(6, role="BACK_SIDE", hint="PREVIOUS", hint_conf=0.9),
        ],
    )
    flat = [p for d in docs for p in d.source_pages]
    assert sorted(flat) == [1, 2, 3, 4, 5, 6]
    assert len(flat) == len(set(flat))  # không overlap


def test_thieu_observation_thi_bao_loi_khong_doan():
    src = make_source("thieu.pdf", [(595.0, 842.0)] * 3)
    with pytest.raises(PipelineError, match="không phủ hết trang"):
        segment_source(src, [obs(1, title="A"), obs(2, title="B")])


def test_observation_trung_trang_bi_tu_choi():
    src = make_source("trung.pdf", [(595.0, 842.0)] * 2)
    with pytest.raises(PipelineError, match="2 observation"):
        segment_source(src, [obs(1, title="A"), obs(1, title="A"), obs(2, title="B")])


def test_khong_doi_thu_tu_trang_khi_xuat():
    src = make_source("order.pdf", [(595.0, 842.0)] * 3)
    docs = segment_source(
        src,
        [
            obs(1, title="A"),
            obs(2, role="CONTINUATION", continues=True),
            obs(3, role="CONTINUATION", continues=True),
        ],
    )
    assert docs[0].source_pages == [1, 2, 3]
