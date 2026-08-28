"""Short-lived staging and atomic PDF creation for local manual imports."""
from __future__ import annotations

import json
import re
import shutil
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import fitz
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

from app.naming import auto_filename
from app.pdf_inventory import sha256_file
from .db import Database
from .taxonomy import TaxonomyAdapter

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
ROTATIONS = {0, 90, 180, 270}
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def ensure_manual_schema(db: Database) -> None:
    with db.session() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS manual_document_events (
          id INTEGER PRIMARY KEY,
          case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
          document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
          filename TEXT NOT NULL, type_id TEXT NOT NULL, document_date TEXT,
          note TEXT, source_files_json TEXT NOT NULL, source_sha256_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_manual_events_case ON manual_document_events(case_id);
        """)


def staging_root(database_path: Path) -> Path:
    path = database_path.parent / "manual-staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _token(token: str) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("Phiên thêm tài liệu không hợp lệ hoặc đã hết hạn.")
    return token


def _manifest(root: Path, token: str) -> dict[str, Any]:
    try:
        result = json.loads((root / _token(token) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Không đọc được phiên xem trước tài liệu.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("pages"), list):
        raise ValueError("Phiên xem trước tài liệu không hợp lệ.")
    return result


def _preview(source: Path, target: Path, page_number: int | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() != ".pdf":
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((420, 560), Image.Resampling.LANCZOS)
            image.save(target, "PNG", optimize=True)
        return
    with fitz.open(source) as pdf:
        page = pdf.load_page((page_number or 1) - 1)
        page.get_pixmap(matrix=fitz.Matrix(.55, .55), alpha=False).save(str(target))


def prepare_uploads(root: Path, case_id: int, uploads: Iterable[Any]) -> dict[str, Any]:
    token = uuid.uuid4().hex
    folder = root / token
    sources, previews = folder / "sources", folder / "previews"
    sources.mkdir(parents=True, exist_ok=False)
    pages: list[dict[str, Any]] = []
    source_info: list[dict[str, str]] = []
    try:
        for source_number, upload in enumerate(uploads, 1):
            original_name = Path(upload.filename or "").name
            extension = Path(original_name).suffix.lower()
            if not original_name or extension not in ALLOWED_EXTENSIONS:
                raise ValueError(f"Định dạng không được hỗ trợ: {original_name or 'file trống'}")
            stored = sources / f"source-{source_number:03d}{extension}"
            with stored.open("wb") as handle:
                shutil.copyfileobj(upload.file, handle)
            source_info.append({"name": original_name, "stored": stored.name, "sha256": sha256_file(stored)})
            if extension == ".pdf":
                try:
                    count = len(PdfReader(str(stored)).pages)
                except Exception as exc:
                    raise ValueError(f"Không đọc được PDF: {original_name}") from exc
                if count < 1:
                    raise ValueError(f"PDF không có trang: {original_name}")
                source_pages = range(1, count + 1)
            else:
                source_pages = [None]
            for page_number in source_pages:
                page_id = f"p{len(pages) + 1}"
                preview_name = f"{page_id}.png"
                _preview(stored, previews / preview_name, page_number)
                pages.append({"page_id": page_id, "source_name": original_name, "source_path": stored.name,
                              "source_page": page_number, "preview_name": preview_name})
        if not pages:
            raise ValueError("Hãy chọn ít nhất một ảnh hoặc PDF.")
        (folder / "manifest.json").write_text(json.dumps({"case_id": case_id, "sources": source_info, "pages": pages}, ensure_ascii=False), encoding="utf-8")
        return {"token": token, "pages": [{"id": p["page_id"], "source_name": p["source_name"],
                 "source_page": p["source_page"], "preview_url": f"/manual-documents/{token}/preview/{p['preview_name']}"} for p in pages]}
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def preview_path(root: Path, token: str, name: str) -> Path:
    if Path(name).suffix.lower() != ".png" or not re.fullmatch(r"p[0-9]+", Path(name).stem):
        raise ValueError("Ảnh xem trước không hợp lệ.")
    base = (root / _token(token) / "previews").resolve()
    path = (base / Path(name).name).resolve()
    path.relative_to(base)
    if not path.is_file():
        raise FileNotFoundError("Không tìm thấy ảnh xem trước.")
    return path


def _next_filename(folder: Path, taxonomy: TaxonomyAdapter, type_id: str) -> tuple[str, list[tuple[Path, Path]]]:
    taxonomy.require(type_id)
    plain_name = auto_filename(taxonomy.catalog, type_id)
    base = Path(plain_name).stem
    plain = folder / plain_name
    numbered: dict[int, Path] = {}
    for candidate in folder.glob(f"{base}.*.pdf"):
        suffix = candidate.stem[len(base) + 1:]
        if suffix.isdigit():
            numbered[int(suffix)] = candidate
    if numbered:
        return auto_filename(taxonomy.catalog, type_id, max(numbered) + 1), []
    if plain.exists():
        return auto_filename(taxonomy.catalog, type_id, 2), [(plain, folder / auto_filename(taxonomy.catalog, type_id, 1))]
    return plain_name, []


def _add_page(writer: PdfWriter, source: Path, source_page: int | None, rotation: int) -> None:
    if source.suffix.lower() == ".pdf":
        reader = PdfReader(str(source))
        page = reader.pages[(source_page or 1) - 1]
        if rotation:
            page.rotate(rotation)
        writer.add_page(page)
        return
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        if rotation:
            image = image.rotate(-rotation, expand=True)
        buffer = BytesIO()
        image.save(buffer, "PDF", resolution=150.0)
        buffer.seek(0)
        writer.add_page(PdfReader(buffer).pages[0])


def save_document(root: Path, db: Database, taxonomy: TaxonomyAdapter, data_root: Path, case_id: int,
                  token: str, page_order: list[dict[str, Any]], type_id: str,
                  document_date: str | None = None, note: str | None = None) -> dict[str, Any]:
    if not taxonomy.is_valid(type_id) or type_id == "UNKNOWN":
        raise ValueError("Hãy chọn một loại tài liệu trong danh mục 104 loại.")
    manifest = _manifest(root, token)
    if int(manifest.get("case_id", -1)) != int(case_id):
        raise ValueError("Phiên thêm tài liệu không thuộc hồ sơ đã chọn.")
    by_id = {p["page_id"]: p for p in manifest["pages"]}
    ids = [str(p.get("id")) for p in page_order]
    if not ids or len(ids) != len(set(ids)) or any(i not in by_id for i in ids):
        raise ValueError("Danh sách trang xem trước không hợp lệ.")
    case = db.one("SELECT folder_path,folder_name FROM cases WHERE id=? AND is_present=1", (case_id,))
    if case is None:
        raise ValueError("Không tìm thấy hồ sơ đích.")
    folder = Path(case["folder_path"]).resolve()
    folder.relative_to(data_root.resolve())
    folder.mkdir(parents=True, exist_ok=True)
    filename, renames = _next_filename(folder, taxonomy, type_id)
    target = folder / filename
    if target.exists():
        raise FileExistsError("Không thể đặt tên tài liệu vì đã có file trùng.")
    writer = PdfWriter()
    for requested in page_order:
        p = by_id[str(requested["id"])]
        rotation = int(requested.get("rotation", 0)) % 360
        if rotation not in ROTATIONS:
            raise ValueError("Góc xoay chỉ được là 0, 90, 180 hoặc 270 độ.")
        source_root = (root / token / "sources").resolve()
        source = (source_root / p["source_path"]).resolve()
        source.relative_to(source_root)
        _add_page(writer, source, p.get("source_page"), rotation)
    writer.add_metadata({"/Producer": "HosoManager", "/Creator": "HosoManager"})
    temp = folder / f".{uuid.uuid4().hex}.part"
    staged: list[tuple[Path, Path]] = []
    try:
        with temp.open("wb") as handle:
            writer.write(handle)
            handle.flush()
        if len(PdfReader(str(temp)).pages) != len(page_order):
            raise ValueError("Không xác minh được PDF đầu ra.")
        for old, new in renames:
            temporary = folder / f".{uuid.uuid4().hex}.rename"
            old.replace(temporary)
            temporary.replace(new)
            staged.append((new, old))
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        for current, original in reversed(staged):
            if current.exists():
                current.replace(original)
        raise
    source_names = [by_id[i]["source_name"] for i in ids]
    hashes = {s["name"]: s["sha256"] for s in manifest.get("sources", [])}
    with db.session() as conn:
        conn.execute("INSERT INTO manual_document_events(case_id,filename,type_id,document_date,note,source_files_json,source_sha256_json,created_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
                      (case_id, filename, type_id, document_date or None, note or None,
                       json.dumps(source_names, ensure_ascii=False), json.dumps({n: hashes.get(n, "") for n in source_names}, ensure_ascii=False)))
    shutil.rmtree(root / token, ignore_errors=True)
    return {"filename": filename, "path": str(target), "page_count": len(page_order), "type_id": type_id}


def discard(root: Path, token: str) -> None:
    shutil.rmtree(root / _token(token), ignore_errors=True)
