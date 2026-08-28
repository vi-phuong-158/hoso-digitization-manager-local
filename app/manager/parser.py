from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .taxonomy import TaxonomyAdapter


FOLDER_RE = re.compile(
    r"^(?P<m1>[^.]+)\.(?P<m2>[^.]+)\.(?P<m3>[^.]+)\.(?P<m4>[^.]+)\.(?P<m5>[^_]+)_(?P<cccd>[^_]+)_(?P<name>.+)$"
)


@dataclass(frozen=True)
class FolderMetadata:
    folder_name: str
    m1: str | None = None
    m2: str | None = None
    m3: str | None = None
    m4: str | None = None
    m5: str | None = None
    citizen_id: str | None = None
    person_name_raw: str | None = None
    person_name_display: str | None = None
    unit_code: str | None = None
    valid: bool = False


def parse_folder_name(name: str) -> FolderMetadata:
    match = FOLDER_RE.match(name)
    if not match:
        return FolderMetadata(folder_name=name)
    values = match.groupdict()
    display = values["name"].replace("_", " ").strip()
    return FolderMetadata(
        folder_name=name,
        m1=values["m1"],
        m2=values["m2"],
        m3=values["m3"],
        m4=values["m4"],
        m5=values["m5"],
        citizen_id=values["cccd"],
        person_name_raw=values["name"],
        person_name_display=display,
        unit_code=values["m1"],
        valid=True,
    )


@dataclass(frozen=True)
class FilenameMetadata:
    filename: str
    taxonomy_code: str | None = None
    sequence_no: int | None = None
    slug_name: str | None = None
    status: str = "MALFORMED_NAME"
    canonical_match: bool = False


def parse_pdf_filename(filename: str, taxonomy: TaxonomyAdapter) -> FilenameMetadata:
    path = Path(filename)
    if path.suffix.lower() != ".pdf":
        return FilenameMetadata(filename=filename)
    parts = path.stem.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or len(parts[0]) not in {2, 3}:
        return FilenameMetadata(filename=filename)
    code = parts[0].zfill(2) if len(parts[0]) == 2 else parts[0]
    item = taxonomy.get(code)
    sequence: int | None = None
    name_parts = parts[1:]
    if len(name_parts) >= 2 and name_parts[-1].isdigit():
        sequence = int(name_parts[-1])
        name_parts = name_parts[:-1]
    if not name_parts or not all(name_parts):
        return FilenameMetadata(filename=filename, taxonomy_code=code, sequence_no=sequence, status="MALFORMED_NAME")
    slug_name = ".".join(name_parts)
    if item is None:
        return FilenameMetadata(filename=filename, taxonomy_code=code, sequence_no=sequence, slug_name=slug_name, status="FILE_NGOAI_TAXONOMY")
    canonical_slug = item.catalog_filename_slug if hasattr(item, "catalog_filename_slug") else taxonomy.catalog.filename_base(code).split(".", 1)[1]
    return FilenameMetadata(
        filename=filename,
        taxonomy_code=code,
        sequence_no=sequence,
        slug_name=slug_name,
        status="OK",
        canonical_match=slug_name.casefold() == canonical_slug.casefold(),
    )
