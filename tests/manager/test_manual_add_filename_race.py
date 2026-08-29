from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[2] / "app" / "manager" / "templates" / "manual_add.html").read_text(encoding="utf-8")


def test_type_and_case_change_await_filename_before_reading_it_for_summary():
    for selector in ("type_id", "case_id"):
        pattern = re.compile(
            r"\$\('" + selector + r"'\)\.addEventListener\('change', async \(\) => \{ await updateFilename\(\); updateSummary\(\); \}\);"
        )
        assert pattern.search(HTML), f"#{selector} change handler must await updateFilename() before updateSummary() reads it"


def test_update_filename_guards_against_out_of_order_responses():
    # A stale request whose id no longer matches the latest one must be
    # discarded rather than overwriting the filename for the current selection.
    fn = re.search(r"async function updateFilename\(\) \{(.*?)\n\}", HTML, re.S)
    assert fn is not None
    body = fn.group(1)
    assert "let filenameRequestId" not in body  # declared outside, incremented inside
    assert "++filenameRequestId" in body
    assert body.count("requestId !== filenameRequestId") >= 2, "both the fetch and the json() await must re-check staleness"


def test_filename_preview_shows_a_transitional_state_not_a_leftover_value():
    fn = re.search(r"async function updateFilename\(\) \{(.*?)\n\}", HTML, re.S)
    assert fn is not None
    body = fn.group(1)
    assert "Đang tính tên tệp..." in body
