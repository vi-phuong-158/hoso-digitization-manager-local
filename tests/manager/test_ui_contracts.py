from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.manager.config import Settings
from app.manager.main import create_app


def _make_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    with path.open("wb") as handle:
        writer.write(handle)


def _setup_app_client(tmp_path: Path) -> tuple[TestClient, int]:
    root = tmp_path / "done" / "output"
    folder = root / "SYNTHETIC_NGUYEN_VAN_A"
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


def test_all_pages_render_http_200(tmp_path: Path):
    client, case_id = _setup_app_client(tmp_path)

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

    for route in routes:
        resp = client.get(route)
        assert resp.status_code == 200, f"Route {route} failed with status {resp.status_code}"
        assert "text/html" in resp.headers["content-type"]
        assert "HỒ SƠ ĐẢNG VIÊN" in resp.text
        assert "/static/manager.css" in resp.text


def test_zero_external_cdn_dependencies(tmp_path: Path):
    client, case_id = _setup_app_client(tmp_path)

    forbidden_patterns = [
        re.compile(r"fonts\.googleapis\.com", re.IGNORECASE),
        re.compile(r"fonts\.gstatic\.com", re.IGNORECASE),
        re.compile(r"cdnjs\.cloudflare\.com", re.IGNORECASE),
        re.compile(r"cdn\.jsdelivr\.net", re.IGNORECASE),
        re.compile(r"unpkg\.com", re.IGNORECASE),
        re.compile(r"http://|https://(?!localhost|127\.0\.0\.1)", re.IGNORECASE),
    ]

    # Check all HTML responses
    routes = ["/", "/cases", f"/cases/{case_id}", "/add-document", "/scan", "/reviews", f"/reviews/{case_id}", "/backup", "/settings"]
    for route in routes:
        html = client.get(route).text
        # Strip internal data: URIs and plain schema URLs if any
        # Check script / link src/href
        for match in re.finditer(r'(?:src|href)=["\']([^"\']+)["\']', html):
            url = match.group(1)
            assert not url.startswith("http://") and not url.startswith("https://"), f"External URL {url} found in {route}"

    # Check CSS file directly
    css_path = Path(__file__).resolve().parents[2] / "app" / "manager" / "static" / "manager.css"
    css_text = css_path.read_text(encoding="utf-8")
    for pattern in [re.compile(r"https?://"), re.compile(r"fonts\.googleapis\.com")]:
        # Filter out comments mentioning URLs
        code_lines = [line for line in css_text.splitlines() if not line.strip().startswith("/*") and not line.strip().startswith("*")]
        for line in code_lines:
            assert pattern.search(line) is None, f"Forbidden external pattern found in CSS line: {line}"


def test_be_vietnam_pro_local_fonts_bundled():
    static_dir = Path(__file__).resolve().parents[2] / "app" / "manager" / "static"
    fonts_dir = static_dir / "fonts"
    assert fonts_dir.is_dir(), "Fonts directory must exist"

    required_fonts = [
        "BeVietnamPro-Regular.woff2",
        "BeVietnamPro-Medium.woff2",
        "BeVietnamPro-SemiBold.woff2",
        "BeVietnamPro-Bold.woff2",
        "OFL.txt",
    ]

    for font_file in required_fonts:
        target = fonts_dir / font_file
        assert target.is_file(), f"Font file {font_file} missing from {fonts_dir}"
        assert target.stat().st_size > 0, f"Font file {font_file} is empty"

    css_path = static_dir / "manager.css"
    css_content = css_path.read_text(encoding="utf-8")
    assert '@font-face' in css_content
    assert '"Be Vietnam Pro"' in css_content
    assert '/static/fonts/BeVietnamPro-Regular.woff2' in css_content
    assert '/static/fonts/BeVietnamPro-Bold.woff2' in css_content


def test_pdf_view_link_contract(tmp_path: Path):
    client, case_id = _setup_app_client(tmp_path)
    detail_html = client.get(f"/cases/{case_id}").text

    assert 'href="/documents/1/view"' in detail_html
    assert 'target="_blank"' in detail_html
    assert 'rel="noopener"' in detail_html

    # Test PDF streaming route
    pdf_resp = client.get("/documents/1/view")
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"].startswith("application/pdf")
    assert pdf_resp.headers["content-disposition"] == "inline"
