from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.manager.config import Settings
from app.manager.main import create_app
from app.manager.ui_labels import (
    ALL_UI_LABELS,
    EVENT_TYPE_LABELS,
    PARSE_STATUS_LABELS,
    STATUS_LABELS,
    WARNING_TYPE_LABELS,
    format_date,
    format_datetime,
    ui_label,
)


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignore = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in ("script", "style"):
            self._ignore = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style"):
            self._ignore = False

    def handle_data(self, data: str) -> None:
        if not self._ignore:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def _extract_visible_text(html: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(html)
    return parser.text()


def _make_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    with path.open("wb") as handle:
        writer.write(handle)


def _setup_client(tmp_path: Path) -> tuple[TestClient, int]:
    root = tmp_path / "done" / "output"
    folder = root / "01.01.01.01.01_012345678901_NGUYEN_VAN_A_CHI_BO_1"
    folder.mkdir(parents=True)
    _make_pdf(folder / "05.Quyet_dinh_ket_nap_dang_vien.pdf")

    cfg = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    app = create_app(cfg)
    client = TestClient(app)

    token = client.get("/").cookies.get("csrf_token")
    assert token
    scan_resp = client.post("/scan", headers={"x-csrf-token": token})
    assert scan_resp.status_code == 200

    listing = client.get("/cases?format=json").json()
    case_id = listing["items"][0]["id"]
    return client, case_id


def test_ui_label_mappings():
    assert ui_label("CHO_XAC_MINH") == "Chờ xác minh"
    assert ui_label("DU_TAI_LIEU") == "Đủ tài liệu"
    assert ui_label("CO_TAI_LIEU") == "Có tài liệu"
    assert ui_label("KHONG_PHAT_SINH") == "Không phát sinh"
    assert ui_label("CHUA_XAC_DINH") == "Chưa xác định"
    assert ui_label("CAN_BO_SUNG") == "Cần bổ sung"
    assert ui_label("CHO_KIEM_TRA") == "Chờ kiểm tra"
    assert ui_label("DANG_SO_HOA") == "Đang số hóa"
    assert ui_label("CHUA_XU_LY") == "Chưa xử lý"
    assert ui_label("HOAN_THANH") == "Hoàn thành"
    assert ui_label("OK") == "Hợp lệ"
    assert ui_label("MALFORMED_NAME") == "Tên tệp không chuẩn"
    assert ui_label("SAI_TEN_THU_MUC") == "Sai tên thư mục"
    assert ui_label("SAI_TEN_FILE") == "Sai tên tệp"
    assert ui_label("FILE_KHONG_DOC_DUOC") == "Không đọc được tệp"
    assert ui_label("FILE_BI_THIEU") == "Tệp bị thiếu"
    assert ui_label("AUTO_STATUS_CHANGED") == "Tự động cập nhật trạng thái"
    assert ui_label("UNKNOWN_FUTURE_CODE") == "Chưa xác định"
    assert ui_label(None) == "Chưa xác định"
    assert ui_label("") == ""


def test_datetime_and_date_formatters():
    assert format_datetime("2026-08-28T15:39:50.123456+00:00") == "28/08/2026 15:39"
    assert format_datetime("2026-08-28T15:39:50") == "28/08/2026 15:39"
    assert format_datetime(None) == "Chưa quét"
    assert format_date("2026-08-28") == "28/08/2026"
    assert format_date(None) == "—"


def test_zero_raw_enums_in_all_rendered_pages(tmp_path: Path):
    client, case_id = _setup_client(tmp_path)
    routes = [
        "/",
        "/cases",
        f"/cases/{case_id}",
        "/add-document",
        "/manual",
        "/scan",
        "/reviews",
        f"/reviews/{case_id}",
        "/backup",
        "/settings",
    ]

    raw_enum_re = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")

    for route in routes:
        resp = client.get(route)
        assert resp.status_code == 200, f"Route {route} failed with {resp.status_code}"
        visible_text = _extract_visible_text(resp.text)

        forbidden_raw = [
            "CHO_XAC_MINH",
            "DU_TAI_LIEU",
            "CO_TAI_LIEU",
            "KHONG_PHAT_SINH",
            "CHUA_XAC_DINH",
            "CAN_BO_SUNG",
            "CHO_KIEM_TRA",
            "DANG_SO_HOA",
            "CHUA_XU_LY",
            "HOAN_THANH",
            "MALFORMED_NAME",
            "SAI_TEN_THU_MUC",
            "SAI_TEN_FILE",
            "AUTO_STATUS_CHANGED",
        ]
        for raw in forbidden_raw:
            assert raw not in visible_text, f"Raw machine code '{raw}' found in visible text of {route}"

        matches = raw_enum_re.findall(visible_text)
        assert len(matches) == 0, f"Raw enum identifiers found in visible text of {route}: {matches}"


def test_scan_terminology_and_zero_ai_mentions(tmp_path: Path):
    client, case_id = _setup_client(tmp_path)
    
    # 1. Base navigation contains "Quét dữ liệu" and NO "AI" / "Trí tuệ nhân tạo"
    resp_dash = client.get("/")
    dash_text = _extract_visible_text(resp_dash.text)
    assert "Quét dữ liệu" in dash_text
    assert "Quét / AI" not in dash_text
    assert "Quét / Trí tuệ nhân tạo" not in dash_text
    assert "Scan / AI" not in dash_text

    # 2. /scan page contains local scan explanation and privacy message
    resp_scan = client.get("/scan")
    scan_text = _extract_visible_text(resp_scan.text)
    assert "Quét kho hồ sơ" in scan_text
    assert "Hoạt động cục bộ" in scan_text
    assert "không tải tài liệu lên Internet" in scan_text
    assert "Thư mục lưu trữ hồ sơ:" in scan_text
    assert "Quét lại toàn bộ kho hồ sơ" in scan_text
    assert "Trí tuệ nhân tạo" not in scan_text
    assert "AI" not in scan_text
    assert "Pipeline" not in scan_text
    assert "HosoManager Local" not in scan_text
    assert "Data Root" not in scan_text


def test_zero_unintended_english_and_forbidden_terms(tmp_path: Path):
    client, case_id = _setup_client(tmp_path)
    routes = [
        "/",
        "/cases",
        f"/cases/{case_id}",
        "/add-document",
        "/manual",
        "/scan",
        "/reviews",
        f"/reviews/{case_id}",
        "/backup",
        "/settings",
    ]

    forbidden_patterns = [
        re.compile(r"\bAI\b"),
        re.compile(r"\bTrí tuệ nhân tạo\b", re.IGNORECASE),
        re.compile(r"\bArtificial Intelligence\b", re.IGNORECASE),
        re.compile(r"\bLocal\s*/\s*Offline\b", re.IGNORECASE),
        re.compile(r"\bData\s*Root\b", re.IGNORECASE),
        re.compile(r"\bReview\s*Required\b", re.IGNORECASE),
        re.compile(r"\bPending\b", re.IGNORECASE),
    ]

    for route in routes:
        resp = client.get(route)
        visible_text = _extract_visible_text(resp.text)

        for pattern in forbidden_patterns:
            match = pattern.search(visible_text)
            assert match is None, f"Forbidden pattern '{pattern.pattern}' found in {route}: '{match.group(0) if match else ''}'"


def test_vietnamese_diacritics_rendered_properly(tmp_path: Path):
    client, case_id = _setup_client(tmp_path)
    resp = client.get(f"/cases/{case_id}")
    assert resp.status_code == 200
    html = resp.text

    assert "HỒ SƠ ĐẢNG VIÊN" in html
    assert "Quản lý số hóa nội bộ" in html
    assert "Hoạt động cục bộ" in html
    assert "Checklist 104 loại tài liệu" in html
    assert "Tài liệu thực tế trong hồ sơ" in html
    assert "Nhật ký số hóa" in html
    assert "Cảnh báo nghiệp vụ" in html
