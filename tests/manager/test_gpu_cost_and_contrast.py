from __future__ import annotations

import re
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parents[2] / "app" / "manager" / "static" / "manager.css"


def _block(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match is not None, f"selector {selector} not found in manager.css"
    return match.group(1)


# ---------------------------------------------------------------------------
# P1-4: backdrop-filter must be gone from the heavily-repeated working
# surfaces, but still present on the header and mobile drawer.
# ---------------------------------------------------------------------------


def test_panel_no_longer_uses_backdrop_filter():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert "backdrop-filter" not in _block(css, ".panel")


def test_header_and_drawer_keep_backdrop_filter():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert "backdrop-filter" in _block(css, ".floating-header")
    assert "backdrop-filter" in _block(css, ".mobile-drawer")
    assert "backdrop-filter" in _block(css, ".drawer-backdrop")


# ---------------------------------------------------------------------------
# P1-5: two tokens measured below WCAG AA (4.5:1) for normal text must now
# clear it. Recomputed here independently rather than trusting the CSS.
# ---------------------------------------------------------------------------


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def _contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _token(css: str, name: str) -> str:
    match = re.search(re.escape(name) + r":\s*(#[0-9A-Fa-f]{6})", css)
    assert match is not None, f"token {name} not found"
    return match.group(1)


def test_ink_400_now_clears_aa_on_white():
    css = CSS_PATH.read_text(encoding="utf-8")
    value = _token(css, "--ink-400")
    assert _contrast_ratio(value, "#ffffff") >= 4.5, f"--ink-400 ({value}) still fails WCAG AA for normal text"


def test_status_supplement_text_clears_aa_on_its_own_background():
    css = CSS_PATH.read_text(encoding="utf-8")
    fg = _token(css, "--status-supplement-text")
    bg = _token(css, "--status-supplement-bg")
    assert _contrast_ratio(fg, bg) >= 4.5


def test_missing_count_cell_no_longer_uses_the_failing_literal_hex():
    html = (Path(__file__).resolve().parents[2] / "app" / "manager" / "templates" / "cases.html").read_text(encoding="utf-8")
    assert "#b78118" not in html
    assert "var(--status-supplement-text)" in html
