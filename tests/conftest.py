from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.catalog import load_catalog  # noqa: E402
from app.models import PageGeometry, PageObservation, TypeCandidate  # noqa: E402
from app.pdf_inventory import SourceFile  # noqa: E402


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def catalog():
    return load_catalog()


def make_source(name: str, sizes: list[tuple[float, float]]) -> SourceFile:
    """SourceFile tổng hợp (không cần file thật) cho unit test segmenter."""
    return SourceFile(
        path=Path(name),
        name=name,
        sha256="0" * 64,
        pages=len(sizes),
        size_bytes=0,
        geometry={i: PageGeometry(w, h) for i, (w, h) in enumerate(sizes, start=1)},
    )


def obs(
    page: int,
    role: str = "CONTENT",
    title: str | None = None,
    date: str | None = None,
    date_conf: float = 0.0,
    candidates: list[tuple[str, float]] | None = None,
    starts: bool | None = None,
    continues: bool = False,
    hint: str = "NONE",
    hint_conf: float = 0.0,
) -> PageObservation:
    if starts is None:
        starts = role == "CONTENT" and not continues
    return PageObservation(
        page_number=page,
        page_role=role,  # type: ignore[arg-type]
        title_guess=title,
        document_date=date,
        date_confidence=date_conf,
        type_candidates=[TypeCandidate(t, c) for t, c in (candidates or [])],
        starts_new_document=starts,
        continues_previous=continues,
        attach_hint=hint,  # type: ignore[arg-type]
        attach_hint_confidence=hint_conf,
    )
