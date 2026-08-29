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


# ---------------------------------------------------------------------------
# Round 1.5: the full nav (brand + 7 links + status pill) measured ~1281px
# of required app-shell content width in a real browser. The hamburger
# breakpoint must sit at or above that so 1180/1100/1024/980px never
# overflow, while 1366/1920 keep the full nav unchanged. .grid-2's own
# breakpoint is a separate, unrelated content-density choice and must not
# have been dragged along with this fix.
# ---------------------------------------------------------------------------


def _media_query_block(css: str, max_width: int) -> str:
    pattern = re.compile(r"@media \(max-width:\s*" + str(max_width) + r"px\)\s*\{(.*?)\n\}\n", re.S)
    match = pattern.search(css)
    assert match is not None, f"@media (max-width: {max_width}px) block not found"
    return match.group(1)


def test_nav_collapses_at_a_breakpoint_that_actually_fits_measured_content():
    css = CSS_PATH.read_text(encoding="utf-8")
    # Measured natural minimum: brand 195.7 + nav-links 815.8 + status 153.4
    # + 2*16 gap + 2*18 padding = 1232.9px header width, needing >= 1280.9px
    # of app-shell content width (app-shell padding is 48px total).
    measured_minimum_viewport = 195.7 + 815.8 + 153.4 + 2 * 16 + 2 * 18 + 48
    assert measured_minimum_viewport <= 1281, "sanity check on the measurement itself"

    block = _media_query_block(css, 1300)
    assert 1300 >= measured_minimum_viewport, "the breakpoint must clear the measured minimum with margin"
    assert ".nav-links" in block and "display: none" in block
    assert ".nav-toggle" in block and "display: inline-flex" in block
    assert ".header-status" in block and "display: none" in block


def test_grid_2_breakpoint_is_independent_of_the_nav_fix():
    css = CSS_PATH.read_text(encoding="utf-8")
    nav_block = _media_query_block(css, 1300)
    assert ".grid-2" not in nav_block, ".grid-2 must not be coupled to the nav breakpoint - unrelated concern"
    grid_block = _media_query_block(css, 980)
    assert ".grid-2" in grid_block
    assert ".nav-links" not in grid_block and ".header-status" not in grid_block


def test_no_desktop_breakpoint_regressed_below_1300():
    # 1366x768 and 1920x1080 must keep showing the full nav: there must be
    # no max-width media query between 1300 and 1920 that also hides it.
    css = CSS_PATH.read_text(encoding="utf-8")
    for width in re.findall(r"@media \(max-width:\s*(\d+)px\)", css):
        w = int(width)
        if 1300 < w < 1920:
            block = _media_query_block(css, w)
            assert ".nav-links" not in block, f"unexpected nav-links rule in @media (max-width: {w}px)"
