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


def test_zero_window_alert_in_templates():
    templates_dir = Path(__file__).resolve().parents[2] / "app" / "manager" / "templates"
    for template_path in templates_dir.glob("*.html"):
        content = template_path.read_text(encoding="utf-8")
        assert re.search(r"\balert\(", content) is None, f"Found alert() call in template {template_path.name}"
        assert "window.alert" not in content, f"Found window.alert in template {template_path.name}"


def test_mobile_navigation_drawer_contracts(tmp_path: Path):
    client, _ = _setup_app_client(tmp_path)
    html = client.get("/").text

    # Verify toggle button
    assert 'id="nav-toggle"' in html
    assert 'class="nav-toggle"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="mobile-nav-drawer"' in html
    assert 'aria-label="Mở menu điều hướng"' in html

    # Verify drawer
    assert 'id="mobile-nav-drawer"' in html
    assert 'class="mobile-drawer"' in html
    assert 'id="drawer-backdrop"' in html
    assert 'class="drawer-backdrop"' in html
    assert 'id="drawer-close"' in html

    # Verify all 7 nav items exist in mobile drawer
    assert 'class="mobile-nav-link" data-nav="dashboard"' in html
    assert 'class="mobile-nav-link" data-nav="cases"' in html
    assert 'class="mobile-nav-link" data-nav="add-document"' in html
    assert 'class="mobile-nav-link" data-nav="scan"' in html
    assert 'class="mobile-nav-link" data-nav="reviews"' in html
    assert 'class="mobile-nav-link" data-nav="backup"' in html
    assert 'class="mobile-nav-link" data-nav="settings"' in html


def test_checklist_status_css_tokens_exist():
    css_path = Path(__file__).resolve().parents[2] / "app" / "manager" / "static" / "manager.css"
    css_text = css_path.read_text(encoding="utf-8")

    required_classes = [
        ".status-CO_TAI_LIEU",
        ".status-KHONG_PHAT_SINH",
        ".status-CHUA_XAC_DINH",
        ".status-CAN_BO_SUNG",
        ".status-HOAN_THANH",
        ".status-DANG_SO_HOA",
        ".status-CHO_KIEM_TRA",
        ".status-CHUA_XU_LY",
    ]

    for cls in required_classes:
        assert cls in css_text, f"Required status class {cls} missing from manager.css"


def test_party_brand_emblem_assets_and_contracts(tmp_path: Path):
    client, _ = _setup_app_client(tmp_path)
    
    # Static files serve 200
    for path in ("/static/brand/party-emblem.png", "/static/brand/party-emblem-64.png", "/static/brand/party-emblem-32.png", "/static/favicon.ico"):
        resp = client.get(path)
        assert resp.status_code == 200, f"Asset {path} failed with {resp.status_code}"
        assert len(resp.content) > 100

    # Rendered header contains party emblem image
    html = client.get("/").text
    assert '<img src="/static/brand/party-emblem.png"' in html
    assert 'class="brand-emblem"' in html
    
    # Old SVG star brand emblem is removed from header
    assert 'class="brand-emblem" aria-hidden="true">\n          <svg' not in html
