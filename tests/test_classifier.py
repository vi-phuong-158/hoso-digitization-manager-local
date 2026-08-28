"""Chính sách confidence (AGENTS.md mục 5) - confidence của model chỉ là 1 tín hiệu."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.classifier import (
    R_CONFUSABLE,
    R_LOW_CONF,
    R_SECOND_PASS_DISAGREE,
    R_SECOND_PASS_LOW,
    R_SEGMENTATION,
    R_UNKNOWN,
    aggregate_candidates,
    classify_document,
)
from app.models import UNKNOWN, DocumentClassification, LogicalDocument, TypeCandidate
from app.vision_adapter import DocumentVisionProvider, ProviderError
from conftest import obs


class StubProvider(DocumentVisionProvider):
    name = "stub"

    def __init__(self, first: DocumentClassification, second: DocumentClassification | None = None):
        self.first = first
        self.second = second
        self.calls: list[bool] = []

    def analyze_pages(self, pdf_path, page_numbers):  # pragma: no cover - không dùng
        raise NotImplementedError

    def classify_document(self, pdf_path, page_numbers, candidates, *, second_pass=False, taxonomy=None):
        self.calls.append(second_pass)
        if second_pass:
            assert self.second is not None, "second pass không được cấu hình"
            assert taxonomy, "second pass phải nhận mô tả taxonomy liên quan"
            return self.second
        return self.first


def doc(pages=(1,), flags=None):
    return LogicalDocument(
        source_file="a.pdf",
        source_pages=list(pages),
        lead_page=pages[0],
        segmentation_flags=list(flags or []),
    )


def cls(type_id, conf, runner=None, date=None, date_conf=0.0):
    return DocumentClassification(
        type_id=type_id,
        confidence=conf,
        document_date=date,
        date_confidence=date_conf,
        title_short="x",
        runner_up=TypeCandidate(*runner) if runner else None,
    )


def run(provider, catalog, d=None, observations=None):
    return classify_document(
        provider, catalog, Path("a.pdf"), d or doc(), observations or [obs(1, title="x")]
    )


def test_confidence_cao_va_khong_rui_ro_thi_auto(catalog):
    p = StubProvider(cls("05", 0.98, runner=("06", 0.10)))
    r = run(p, catalog)
    assert r.classification_status == "AUTO"
    assert r.classification_reasons == []
    assert p.calls == [False]


def test_confidence_duoi_080_thi_human_review_khong_goi_second_pass(catalog):
    p = StubProvider(cls("86", 0.62))
    r = run(p, catalog)
    assert r.classification_status == "REVIEW"
    assert R_LOW_CONF in r.classification_reasons
    assert p.calls == [False]
    assert r.classification.type_id == "86"  # vẫn giữ nhãn ứng viên để người đọc thấy


def test_bang_080_089_goi_second_pass_va_van_thap_thi_review(catalog):
    p = StubProvider(cls("86", 0.88), cls("86", 0.90))
    r = run(p, catalog)
    assert p.calls == [False, True]
    assert r.second_pass_used
    assert r.classification_status == "REVIEW"
    assert R_SECOND_PASS_LOW in r.classification_reasons


def test_second_pass_nang_len_095_thi_auto(catalog):
    p = StubProvider(cls("05", 0.88), cls("05", 0.97))
    r = run(p, catalog)
    assert r.classification_status == "AUTO"
    assert r.classification.confidence == pytest.approx(0.97)


def test_second_pass_khac_ket_luan_thi_review(catalog):
    p = StubProvider(cls("70", 0.90), cls("86", 0.99))
    r = run(p, catalog)
    assert r.classification_status == "REVIEW"
    assert R_SECOND_PASS_DISAGREE in r.classification_reasons


def test_unknown_luon_human_review(catalog):
    p = StubProvider(cls(UNKNOWN, 0.99))
    r = run(p, catalog)
    assert r.classification_status == "REVIEW"
    assert R_UNKNOWN in r.classification_reasons


def test_segmentation_mo_ho_thi_review_du_confidence_cao(catalog):
    p = StubProvider(cls("05", 0.99))
    r = run(p, catalog, d=doc(flags=["AMBIGUOUS_COVER_BINDING"]))
    assert r.classification_status == "REVIEW"
    assert R_SEGMENTATION in r.classification_reasons


def test_cap_de_nham_cach_biet_hep_thi_review(catalog):
    # 70 vs 86 nằm cùng nhóm dễ nhầm; cách biệt < 0.10 -> không được AUTO.
    p = StubProvider(cls("86", 0.96, runner=("70", 0.93)))
    r = run(p, catalog)
    assert r.classification_status == "REVIEW"
    assert R_CONFUSABLE in r.classification_reasons


def test_cap_de_nham_cach_biet_rong_thi_auto(catalog):
    p = StubProvider(cls("86", 0.96, runner=("70", 0.20)))
    r = run(p, catalog)
    assert r.classification_status == "AUTO"


def test_type_id_ngoai_catalog_bi_chan_o_hang_rao_provider(catalog):
    p = StubProvider(cls("105", 0.99))
    with pytest.raises(ProviderError, match="không có trong catalog"):
        run(p, catalog)


def test_aggregate_candidates_uu_tien_trang_noi_dung():
    observations = [
        obs(1, candidates=[("86", 0.90)]),
        obs(2, role="COVER", candidates=[("70", 0.90)], hint="PREVIOUS"),
    ]
    ranked = aggregate_candidates(observations)
    assert ranked[0].type_id == "86"
    assert ranked[0].confidence == pytest.approx(0.90)
    assert ranked[1].confidence == pytest.approx(0.45)  # trang bìa bị hạ trọng số
