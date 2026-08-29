from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import Database
from .manual_documents import discard, ensure_manual_schema, prepare_uploads, preview_path, save_document, staging_root
from .scanner import ScanService
from .status import mark_complete, recompute_case, reopen, set_checklist_override, set_manual_status, update_note
from .taxonomy import TaxonomyAdapter


def _json_requested(request: Request) -> bool:
    return request.query_params.get("format") == "json" or "application/json" in request.headers.get("accept", "")


def _dict(row) -> dict:
    return dict(row) if row else {}


def dashboard_context(db: Database) -> dict:
    rows = db.all("SELECT * FROM cases WHERE is_present=1")
    counts = {status: 0 for status in ("CHUA_XU_LY", "DANG_SO_HOA", "CHO_KIEM_TRA", "CAN_BO_SUNG", "HOAN_THANH")}
    for row in rows:
        counts[row["effective_status"]] = counts.get(row["effective_status"], 0) + 1
    total = len(rows)
    progress = round(sum(row["progress_percent"] for row in rows) / total, 2) if total else 0.0
    units: dict[str, list[float]] = {}
    for row in rows:
        units.setdefault(row["unit_code"] or "Chưa xác định", []).append(row["progress_percent"])
    unit_rows = [{"unit": unit, "progress": round(sum(values) / len(values), 2), "count": len(values)} for unit, values in sorted(units.items())]
    action_rows = [row for row in rows if row["effective_status"] in {"CAN_BO_SUNG", "DANG_SO_HOA", "CHO_KIEM_TRA"} or row["warning_count"]]
    return {
        "total": total,
        "counts": counts,
        "progress": progress,
        "missing_p1": sum(row["missing_priority1_count"] for row in rows),
        "review_pending": db.one("SELECT COUNT(*) AS n FROM warnings WHERE active=1")["n"],
        "unit_rows": unit_rows,
        "action_rows": action_rows[:10],
        "last_run": _dict(db.one("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1")),
    }


def _case_payload(db: Database, case_id: int) -> dict:
    if db.one("SELECT id FROM cases WHERE id=?", (case_id,)) is None:
        raise LookupError("Không tìm thấy hồ sơ")
    with db.session() as conn:
        state = recompute_case(conn, case_id)
        case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        documents = [dict(item) for item in conn.execute("SELECT * FROM documents WHERE case_id=? AND is_present=1 ORDER BY relative_path", (case_id,)).fetchall()]
        warnings = [dict(item) for item in conn.execute("SELECT * FROM warnings WHERE case_id=? AND active=1 ORDER BY severity DESC,id", (case_id,)).fetchall()]
        history = [dict(item) for item in conn.execute("SELECT * FROM case_history WHERE case_id=? ORDER BY id DESC", (case_id,)).fetchall()]
    return {"case": dict(case), "documents": documents, "warnings": warnings, "history": history, "checklist": state["checklist"]}


def register_routes(app, cfg: Settings, db: Database, templates: Jinja2Templates) -> None:
    from .main import _csrf_valid, _payload

    taxonomy = TaxonomyAdapter.load()
    taxonomy.seed(db)
    scanner = ScanService(cfg, db, taxonomy)
    ensure_manual_schema(db)

    @app.get("/add-document")
    @app.get("/manual")
    def add_document(request: Request, case_id: int | None = None):
        rows = [dict(row) for row in db.all("SELECT id,folder_name,person_name_display FROM cases WHERE is_present=1 ORDER BY COALESCE(person_name_display,folder_name)")]
        context = {"cases": rows, "selected_case_id": case_id, "taxonomy": taxonomy.as_dicts()}
        return context if _json_requested(request) else templates.TemplateResponse(request=request, name="manual_add.html", context=context)

    @app.post("/manual-documents/prepare")
    async def manual_prepare(request: Request, case_id: int = Form(...), files: list[UploadFile] = File(...)):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        if db.one("SELECT id FROM cases WHERE id=? AND is_present=1", (case_id,)) is None:
            return JSONResponse({"detail": "Không tìm thấy hồ sơ đích"}, status_code=404)
        try:
            return prepare_uploads(staging_root(cfg.database_path), case_id, files)
        except (OSError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        finally:
            for upload in files:
                await upload.close()

    @app.get("/manual-documents/name-preview")
    def manual_name_preview(case_id: int, type_id: str):
        case = db.one("SELECT folder_path FROM cases WHERE id=? AND is_present=1", (case_id,))
        if case is None:
            return JSONResponse({"detail": "Không tìm thấy hồ sơ đích"}, status_code=404)
        try:
            from .manual_documents import _next_filename
            filename, _ = _next_filename(Path(case["folder_path"]).resolve(), taxonomy, type_id)
            return {"filename": filename}
        except (OSError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/manual-documents/{token}/preview/{preview_name}")
    def manual_preview(token: str, preview_name: str):
        try:
            return FileResponse(preview_path(staging_root(cfg.database_path), token, preview_name), media_type="image/png", headers={"Cache-Control": "private, no-store"})
        except (OSError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.delete("/manual-documents/{token}")
    async def manual_discard(request: Request, token: str):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        try:
            discard(staging_root(cfg.database_path), token)
            return {"ok": True}
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/manual-documents/save")
    async def manual_save(request: Request):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        try:
            payload = await _payload(request)
            case_id_value = int(payload.get("case_id"))
            result = save_document(staging_root(cfg.database_path), db, taxonomy, cfg.data_root, case_id_value,
                str(payload.get("token")), payload.get("pages") or [], str(payload.get("type_id")),
                str(payload.get("document_date") or "") or None, str(payload.get("note") or "") or None)
            scanner.scan(case_id_value)
            _recompute_all(db, case_id_value)
            return result
        except (OSError, ValueError, TypeError, IndexError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/cases")
    def cases(request: Request, q: str = "", status: str = "", unit: str = "", warning: int = 0, missing_p1: int = 0, page: int = 1, page_size: int = 50, sort: str = "updated"):
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        where, params = ["c.is_present=1"], []
        if q:
            where.append("(c.person_name_display LIKE ? OR c.person_name_raw LIKE ? OR c.citizen_id LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        if status:
            where.append("c.effective_status=?"); params.append(status)
        if unit:
            where.append("c.unit_code=?"); params.append(unit)
        if warning:
            where.append("EXISTS (SELECT 1 FROM warnings w WHERE w.case_id=c.id AND w.active=1)")
        if missing_p1:
            where.append("c.missing_priority1_count>0")
        where_sql = " AND ".join(where)
        total = db.one(f"SELECT COUNT(*) AS n FROM cases c WHERE {where_sql}", tuple(params))["n"]
        total_pages = max(1, -(-total // page_size))
        page = min(page, total_pages)
        order = {"updated": "c.last_scanned_at DESC", "progress": "c.progress_percent ASC", "name": "COALESCE(c.person_name_display,c.folder_name) COLLATE NOCASE"}.get(sort, "c.last_scanned_at DESC")
        list_params = tuple(params) + (page_size, (page - 1) * page_size)
        rows = [dict(row) for row in db.all(f"SELECT c.* FROM cases c WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?", list_params)]
        filter_params: dict[str, str | int] = {}
        if q: filter_params["q"] = q
        if status: filter_params["status"] = status
        if unit: filter_params["unit"] = unit
        if warning: filter_params["warning"] = 1
        if missing_p1: filter_params["missing_p1"] = 1
        if sort != "updated": filter_params["sort"] = sort
        if page_size != 50: filter_params["page_size"] = page_size
        base_qs = urlencode(filter_params)
        context = {
            "rows": rows, "q": q, "status": status, "unit": unit, "warning": warning, "missing_p1": missing_p1, "sort": sort,
            "page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "base_qs": base_qs,
            "units": [row["unit_code"] for row in db.all("SELECT DISTINCT unit_code FROM cases WHERE is_present=1 AND unit_code IS NOT NULL ORDER BY unit_code")],
        }
        return ({"items": rows, "page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "count": len(rows)}
                if _json_requested(request) else templates.TemplateResponse(request=request, name="cases.html", context=context))

    @app.get("/reviews")
    def reviews(request: Request):
        rows = [dict(row) for row in db.all("SELECT c.id,c.folder_name,c.person_name_display,c.effective_status,COUNT(w.id) AS warning_count FROM cases c LEFT JOIN warnings w ON w.case_id=c.id AND w.active=1 WHERE c.is_present=1 GROUP BY c.id ORDER BY c.folder_name")]
        context = {"rows": rows}
        return context if _json_requested(request) else templates.TemplateResponse(request=request, name="reviews.html", context=context)

    @app.get("/reviews/{case_id}")
    def review_case_detail(request: Request, case_id: int):
        try:
            payload = _case_payload(db, case_id)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        return payload if _json_requested(request) else templates.TemplateResponse(request=request, name="review_case.html", context=payload)

    @app.get("/cases/{case_id}")
    def case_detail(request: Request, case_id: int):
        try:
            payload = _case_payload(db, case_id)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        return payload if _json_requested(request) else templates.TemplateResponse(request=request, name="case_detail.html", context=payload)

    @app.post("/cases/{case_id}/status")
    async def case_status(request: Request, case_id: int):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        try:
            with db.session() as conn: return set_manual_status(conn, case_id, (await _payload(request)).get("status", ""))
        except ValueError as exc: return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/cases/{case_id}/complete")
    async def complete_case(request: Request, case_id: int):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        try:
            with db.session() as conn: return mark_complete(conn, case_id, (await _payload(request)).get("reviewed_by"))
        except ValueError as exc: return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.post("/cases/{case_id}/reopen")
    async def reopen_case(request: Request, case_id: int):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        try:
            with db.session() as conn: return reopen(conn, case_id)
        except ValueError as exc: return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.post("/cases/{case_id}/checklist/{taxonomy_code}")
    async def checklist_action(request: Request, case_id: int, taxonomy_code: str):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        try:
            with db.session() as conn: return set_checklist_override(conn, case_id, taxonomy_code, payload.get("status", ""), payload.get("note"))
        except ValueError as exc: return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/cases/{case_id}/note")
    async def case_note(request: Request, case_id: int):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        with db.session() as conn: update_note(conn, case_id, (await _payload(request)).get("note"))
        return {"ok": True}

    @app.post("/scan")
    async def scan_all(request: Request):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        result = scanner.scan(); _recompute_all(db); return result.as_dict()

    @app.post("/scan/{case_id}")
    async def scan_one(request: Request, case_id: int):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        result = scanner.scan(case_id); _recompute_all(db, case_id); return result.as_dict()

    @app.get("/scan")
    def scan_page(request: Request):
        context = {"settings": cfg}
        return {"available": True, "message": "AI processing thuộc công cụ Pipeline riêng; Manager chỉ quét metadata local."} if _json_requested(request) else templates.TemplateResponse(request=request, name="scan.html", context=context)

    @app.get("/backup")
    def backup_page(request: Request):
        if _json_requested(request):
            return {"data_root": str(cfg.data_root), "database_path": str(cfg.database_path)}
        return templates.TemplateResponse(request=request, name="backup.html", context={"data_root": cfg.data_root, "database_path": cfg.database_path})

    @app.post("/backup")
    async def backup_now(request: Request):
        if not _csrf_valid(request): return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        return _backup(db, cfg)

    @app.get("/scan-runs")
    def scan_runs(): return {"items": [dict(row) for row in db.all("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 50")]}

    @app.get("/open/case/{case_id}")
    def open_case(case_id: int):
        row = db.one("SELECT folder_path FROM cases WHERE id=?", (case_id,))
        path = _safe_path(cfg.data_root, Path(row["folder_path"])) if row else None
        if path is None or not path.is_dir(): return JSONResponse({"detail": "Thư mục không còn tồn tại"}, status_code=404)
        _open_local(path); return {"ok": True}

    @app.get("/documents/{document_id}/view")
    def view_document(document_id: int):
        row = db.one("SELECT relative_path FROM documents WHERE id=? AND is_present=1", (document_id,))
        path = _safe_path(cfg.data_root, cfg.data_root / Path(row["relative_path"])) if row else None
        if path is None or not path.is_file() or path.suffix.lower() != ".pdf":
            return JSONResponse({"detail": "Không tìm thấy tài liệu trên máy."}, status_code=404)
        return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})

    @app.get("/open/document/{document_id}")
    def open_document(document_id: int):
        return RedirectResponse(url=f"/documents/{document_id}/view", status_code=307)


def _backup(db: Database, cfg: Settings) -> dict:
    # Timestamped (to the microsecond) so each backup is its own file instead
    # of silently overwriting the previous one - the operator's only safety
    # net if the live database is corrupted between backups.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = cfg.database_path.with_name(f"{cfg.database_path.stem}.backup.{stamp}.sqlite")
    db.backup_to(target)
    return {"ok": True, "path": str(target), "metadata_only": True}


def _recompute_all(db: Database, only_case_id: int | None = None) -> None:
    with db.session() as conn:
        rows = conn.execute("SELECT id FROM cases WHERE is_present=1" + (" AND id=?" if only_case_id else ""), ((only_case_id,) if only_case_id else ())).fetchall()
        for row in rows: recompute_case(conn, row["id"])


def _safe_path(root: Path, path: Path) -> Path | None:
    try:
        candidate = path.resolve(); candidate.relative_to(root.resolve()); return candidate
    except (OSError, ValueError):
        return None


def _open_local(path: Path) -> None:
    if os.name == "nt":
        try: os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError: pass