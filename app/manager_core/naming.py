from __future__ import annotations

from .taxonomy import Taxonomy


def auto_filename(taxonomy: Taxonomy, type_id: str, sequence: int | None = None) -> str:
    base = taxonomy.require(type_id).filename_base
    if sequence is None:
        return f"{base}.pdf"
    if sequence < 1:
        raise ValueError("sequence phải >= 1")
    return f"{base}.{sequence}.pdf"