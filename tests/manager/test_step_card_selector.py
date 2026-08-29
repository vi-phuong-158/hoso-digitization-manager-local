from __future__ import annotations

from pathlib import Path

CSS_PATH = Path(__file__).resolve().parents[2] / "app" / "manager" / "static" / "manager.css"
MANUAL_ADD_HTML = Path(__file__).resolve().parents[2] / "app" / "manager" / "templates" / "manual_add.html"


def test_css_targets_step_card_not_orphaned_step_class():
    css = CSS_PATH.read_text(encoding="utf-8")
    # The markup uses class="step-card"; a bare ".step" selector never matches it.
    assert ".step input[type=text]" not in css
    assert ".step textarea" not in css
    assert ".step-card input[type=text]" in css
    assert ".step-card textarea" in css


def test_manual_add_uses_step_card_class_matching_the_fixed_selector():
    html = MANUAL_ADD_HTML.read_text(encoding="utf-8")
    assert 'class="step-card"' in html
    assert 'id="document_date" type="text"' in html
    assert 'id="note" type="text"' in html
    # These two fields sit inside a .step-card and rely purely on the
    # descendant selector for styling (no .wide-input/.wide-select fallback).
    assert 'id="document_date" type="text" placeholder' in html
