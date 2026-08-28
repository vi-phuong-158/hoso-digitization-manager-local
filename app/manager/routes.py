from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import Database
from .integration import ensure_schema, integrate_case, provider_for
from .manual_documents import discard, ensure_manual_schema, prepare_uploads, preview_path, save_document, staging_root
from .scanner import ScanService
from .status import mark_complete, now as status_now, recompute_case, reopen, set_checklist_override, set_manual_status, update_note
from .taxonomy import TaxonomyAdapter
from app.catalog import load_catalog
from app.review_repair import (
    apply_repair, create_repair_plan, decide_finding, get_repair_plan,
    list_findings, run_semantic_review, sessions_for_person, start_review,
)
from app.state import StateRegistry


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
    units = {}
    for row in rows:
        unit = row["unit_code"] or "Chưa xác định"
        units.setdefault(unit, []).append(row["progress_percent"])
    unit_rows = [{"unit": unit, "progress": round(sum(values) / len(values), 2), "count": len(values)} for unit, values in sorted(units.items())]
    action_rows = [row for row in rows if row["effective_status"] in {"CAN_BO_SUNG", "DANG_SO_HOA", "CHO_KIEM_TRA"} or row["warning_count"]]
    last_run = db.one("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1")
    return {
        "total": total,
        "counts": counts,
        "progress": progress,
        "missing_p1": sum(row["missing_priority1_count"] for row in rows),
        "review_pending": db.one("SELECT COUNT(*) AS n FROM warnings WHERE active=1 AND warning_type IN ('REVIEW_PENDING','CAN_XAC_MINH')")["n"],
        "unit_rows": unit_rows,
        "action_rows": action_rows[:10],
        "last_run": _dict(last_run),
    }


def _case_payload(db: Database, case_id: int) -> dict:
    row = db.one("SELECT * FROM cases WHERE id=?", (case_id,))
    if row is None:
        raise LookupError("Không tìm thấy hồ sơ")
    with db.session() as conn:
        state = recompute_case(conn, case_id)
        case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        documents = [dict(item) for item in conn.execute("SELECT * FROM documents WHERE case_id=? AND is_present=1 ORDER BY relative_path", (case_id,)).fetchall()]
        warnings = [dict(item) for item in conn.execute("SELECT * FROM warnings WHERE case_id=? AND active=1 ORDER BY severity DESC,id", (case_id,)).fetchall()]
        history = [dict(item) for item in conn.execute("SELECT * FROM case_history WHERE case_id=? ORDER BY id DESC", (case_id,)).fetchall()]
        pipeline_documents = [dict(item) for item in conn.execute("SELECT * FROM pipeline_documents WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
    return {"case": dict(case), "documents": documents, "warnings": warnings, "history": history, "pipeline_documents": pipeline_documents, "checklist": state["checklist"]}


def register_routes(app, cfg: Settings, db: Database, templates: Jinja2Templates) -> None:
    from .main import _csrf_valid, _payload
    taxonomy = TaxonomyAdapter.load()
    taxonomy.seed(db)
    scanner = ScanService(cfg, db, taxonomy)
    ensure_schema(db)
    integration_provider = provider_for(cfg, taxonomy)

    def review_roots() -> tuple[Path, Path, Path]:
        root = cfg.data_root.parent
        return root / "state" / "processing_state.db", root / "output", root / "review"

    ensure_manual_schema(db)

    @app.get("/add-document")
    @app.get("/manual")
    def add_document(request: Request, case_id: int | None = None):
        rows = [dict(row) for row in db.all("SELECT id,folder_name,person_name_display FROM cases WHERE is_present=1 ORDER BY COALESCE(person_name_display,folder_name)")]
        context = {"cases": rows, "selected_case_id": case_id, "taxonomy": taxonomy.as_dicts()}
        if _json_requested(request):
            return context
        return templates.TemplateResponse(request=request, name="manual_add.html", context=context)

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
            return FileResponse(preview_path(staging_root(cfg.database_path), token, preview_name), media_type="image/png")
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
            result = save_document(staging_root(cfg.database_path), db, taxonomy, cfg.data_root,
                case_id_value, str(payload.get("token")), payload.get("pages") or [],
                str(payload.get("type_id")), str(payload.get("document_date") or "") or None,
                str(payload.get("note") or "") or None)
            scanner.scan(case_id_value)
            _recompute_all(db, case_id_value)
            return result
        except (OSError, ValueError, TypeError, IndexError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
    @app.get("/cases")
    def cases(request: Request, q: str = "", status: str = "", unit: str = "", warning: int = 0, missing_p1: int = 0, page: int = 1, sort: str = "updated"):
        page = max(page, 1)
        where = ["c.is_present=1"]
        params: list = []
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
        order = {"updated": "c.last_scanned_at DESC", "progress": "c.progress_percent ASC", "name": "COALESCE(c.person_name_display,c.folder_name) COLLATE NOCASE"}.get(sort, "c.last_scanned_at DESC")
        sql = "SELECT c.* FROM cases c WHERE " + " AND ".join(where) + f" ORDER BY {order} LIMIT 100 OFFSET ?"
        params.append((page - 1) * 100)
        rows = [dict(row) for row in db.all(sql, tuple(params))]
        for row in rows:
            row["type_count"] = db.one("SELECT COUNT(DISTINCT taxonomy_code) AS n FROM documents WHERE case_id=? AND is_present=1 AND parse_status='OK' AND taxonomy_code IS NOT NULL", (row["id"],))["n"]
            row["missing_type_count"] = max(0, len(taxonomy.items) - row["type_count"])
        units = [row["unit_code"] for row in db.all("SELECT DISTINCT unit_code FROM cases WHERE is_present=1 AND unit_code IS NOT NULL ORDER BY unit_code")]
        context = {"rows": rows, "q": q, "status": status, "unit": unit, "warning": warning, "missing_p1": missing_p1, "sort": sort, "units": units}
        if _json_requested(request):
            return {"items": rows, "page": page, "count": len(rows)}
        return templates.TemplateResponse(request=request, name="cases.html", context=context)

    @app.get("/reviews")
    def reviews(request: Request):
        state_db, _, _ = review_roots()
        rows = [dict(row) for row in db.all("SELECT id,folder_name,person_name_display,effective_status FROM cases WHERE is_present=1 ORDER BY folder_name")]
        if not state_db.is_file():
            context = {"rows": rows, "state_available": False, "session_counts": {}}
        else:
            with StateRegistry(state_db) as registry:
                context = {"rows": rows, "state_available": True,
                    "session_counts": {row["folder_name"]: len(sessions_for_person(registry, row["folder_name"])) for row in rows}}
        if _json_requested(request):
            return context
        return templates.TemplateResponse(request=request, name="reviews.html", context=context)

    @app.get("/reviews/{case_id}")
    def review_case_detail(request: Request, case_id: int):
        try:
            payload = _case_payload(db, case_id)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        state_db, output_root, review_root = review_roots()
        if not state_db.is_file():
            payload.update({"state_available": False, "sessions": [], "findings": []})
        else:
            with StateRegistry(state_db) as registry:
                sessions = sessions_for_person(registry, payload["case"]["folder_name"])
                latest = sessions[0] if sessions else None
                payload.update({"state_available": True, "sessions": [s.__dict__ for s in sessions],
                    "findings": [f.__dict__ for f in list_findings(registry, latest.session_id)] if latest else [],
                    "output_dir": str(output_root / payload["case"]["folder_name"]), "review_dir": str(review_root / payload["case"]["folder_name"])})
        if _json_requested(request):
            return payload
        return templates.TemplateResponse(request=request, name="review_case.html", context=payload)

    @app.post("/reviews/{case_id}/start")
    def review_start(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        case = db.one("SELECT folder_name,folder_path FROM cases WHERE id=? AND is_present=1", (case_id,))
        if case is None:
            return JSONResponse({"detail": "Không tìm thấy hồ sơ"}, status_code=404)
        state_db, output_root, review_root = review_roots()
        if not state_db.is_file():
            return JSONResponse({"detail": "Chưa có pipeline state; hãy xử lý hồ sơ bằng pipeline trước."}, status_code=409)
        with StateRegistry(state_db) as registry:
            session, _ = start_review(registry, case["folder_name"], catalog=load_catalog(),
                output_dir=output_root / case["folder_name"], review_dir=review_root / case["folder_name"])
        return {"ok": True, "session_id": session.session_id, "message": "Review chỉ tạo findings; chưa thay đổi canonical state."}

    @app.post("/reviews/{case_id}/start-semantic")
    def review_start_semantic(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        if not cfg.semantic_endpoint or not cfg.semantic_model:
            return JSONResponse({"detail": "Semantic review chưa được cấu hình endpoint/model; không có fallback tự động."}, status_code=409)
        case = db.one("SELECT folder_name,folder_path FROM cases WHERE id=? AND is_present=1", (case_id,))
        if case is None:
            return JSONResponse({"detail": "Không tìm thấy hồ sơ"}, status_code=404)
        state_db, output_root, review_root = review_roots()
        if not state_db.is_file():
            return JSONResponse({"detail": "Chưa có pipeline state; hãy xử lý hồ sơ bằng pipeline trước."}, status_code=409)
        from app.semantic_reviewer import OpenAICompatibleSemanticReviewer, PdfToPpmRenderer
        try:
            with StateRegistry(state_db) as registry:
                session, findings = run_semantic_review(registry, case["folder_name"], catalog=load_catalog(),
                    folder=Path(case["folder_path"]), output_dir=output_root / case["folder_name"],
                    review_dir=review_root / case["folder_name"],
                    reviewer=OpenAICompatibleSemanticReviewer(endpoint=cfg.semantic_endpoint, model=cfg.semantic_model,
                        api_key_env=cfg.semantic_api_key_env), renderer=PdfToPpmRenderer())
            return {"ok": True, "session_id": session.session_id, "findings": len(findings), "message": "Semantic review chỉ tạo proposal; chưa thay đổi canonical state."}
        except PipelineError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.post("/reviews/findings/{finding_id}/decision")
    async def review_decision(request: Request, finding_id: str):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        state_db, _, _ = review_roots()
        if not state_db.is_file():
            return JSONResponse({"detail": "Không có pipeline state"}, status_code=409)
        try:
            with StateRegistry(state_db) as registry:
                finding = decide_finding(registry, finding_id, decision=payload.get("decision", ""),
                    reviewer=payload.get("reviewer") or "operator", manual_fix=payload.get("manual_fix"))
            return {"ok": True, "finding": finding.__dict__}
        except PipelineError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/reviews/{case_id}/repair-plan")
    async def review_repair_plan(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        state_db, _, _ = review_roots()
        try:
            with StateRegistry(state_db) as registry:
                plan = create_repair_plan(registry, payload.get("session_id", ""))
            return {"ok": True, "plan": plan.__dict__}
        except PipelineError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/reviews/{case_id}/repair/{plan_id}")
    async def review_repair(request: Request, case_id: int, plan_id: str):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        case = db.one("SELECT folder_name,folder_path FROM cases WHERE id=? AND is_present=1", (case_id,))
        if case is None:
            return JSONResponse({"detail": "Không tìm thấy hồ sơ"}, status_code=404)
        state_db, output_root, review_root = review_roots()
        try:
            with StateRegistry(state_db) as registry:
                result = apply_repair(registry, plan_id, catalog=load_catalog(), folder=Path(case["folder_path"]),
                    output_dir=output_root / case["folder_name"], review_dir=review_root / case["folder_name"],
                    dry_run=not bool(payload.get("apply")))
            return result
        except PipelineError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/cases/{case_id}")
    def case_detail(request: Request, case_id: int):
        try:
            payload = _case_payload(db, case_id)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        if _json_requested(request):
            return payload
        return templates.TemplateResponse(request=request, name="case_detail.html", context=payload)

    @app.post("/cases/{case_id}/status")
    async def case_status(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        try:
            with db.session() as conn:
                result = set_manual_status(conn, case_id, payload.get("status", ""))
            return result
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/cases/{case_id}/complete")
    async def complete_case(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        try:
            with db.session() as conn:
                return mark_complete(conn, case_id, payload.get("reviewed_by"))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.post("/cases/{case_id}/reopen")
    async def reopen_case(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        try:
            with db.session() as conn:
                return reopen(conn, case_id)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.post("/cases/{case_id}/checklist/{taxonomy_code}")
    async def checklist_action(request: Request, case_id: int, taxonomy_code: str):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        try:
            with db.session() as conn:
                return set_checklist_override(conn, case_id, taxonomy_code, payload.get("status", ""), payload.get("note"))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/cases/{case_id}/note")
    async def case_note(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        with db.session() as conn:
            update_note(conn, case_id, payload.get("note"))
        return {"ok": True}

    @app.post("/scan")
    async def scan_all(request: Request):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        result = scanner.scan()
        _integrate_all(db, integration_provider)
        _recompute_all(db)
        return result.as_dict()

    @app.post("/scan/{case_id}")
    async def scan_one(request: Request, case_id: int):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        result = scanner.scan(case_id)
        _integrate_all(db, integration_provider, case_id)
        _recompute_all(db, case_id)
        return result.as_dict()

    @app.get("/scan")
    def scan_page(request: Request):
        if _json_requested(request):
            return {"available": True, "message": "Scan/AI là công cụ riêng của Manager."}
        return templates.TemplateResponse(request=request, name="scan.html", context={"settings": cfg})

    @app.get("/backup")
    def backup_page(request: Request):
        if _json_requested(request):
            target = cfg.database_path.with_name(cfg.database_path.stem + ".backup.sqlite")
            db.backup_to(target)
            return {"ok": True, "path": str(target), "metadata_only": True}
        return templates.TemplateResponse(request=request, name="backup.html", context={"data_root": cfg.data_root, "database_path": cfg.database_path})

    @app.get("/backup/metadata")
    def backup_metadata():
        target = cfg.database_path.with_name(cfg.database_path.stem + ".backup.sqlite")
        db.backup_to(target)
        return {"ok": True, "path": str(target), "metadata_only": True}
    @app.get("/scan-runs")
    def scan_runs():
        return {"items": [dict(row) for row in db.all("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 50")]}

    @app.get("/open/case/{case_id}")
    def open_case(case_id: int):
        row = db.one("SELECT folder_path FROM cases WHERE id=?", (case_id,))
        if row is None:
            return JSONResponse({"detail": "Không tìm thấy hồ sơ"}, status_code=404)
        path = _safe_path(cfg.data_root, Path(row["folder_path"]))
        if path is None or not path.is_dir():
            return JSONResponse({"detail": "Thư mục không còn tồn tại"}, status_code=404)
        _open_local(path)
        return {"ok": True, "path": str(path)}

    @app.get("/open/document/{document_id}")
    def open_document(document_id: int):
        row = db.one("SELECT relative_path,filename FROM documents WHERE id=? AND is_present=1", (document_id,))
        if row is None:
            return JSONResponse({"detail": "Không tìm thấy tài liệu"}, status_code=404)
        path = _safe_path(cfg.data_root, cfg.data_root / Path(row["relative_path"]))
        if path is None or not path.is_file() or path.suffix.lower() != ".pdf":
            return JSONResponse({"detail": "Tài liệu không hợp lệ"}, status_code=404)
        return FileResponse(path, media_type="application/pdf", filename=row["filename"])


def _integrate_all(db: Database, provider, only_case_id: int | None = None) -> None:
    if provider.__class__.__name__ == "NoopProvider":
        return
    rows = db.all("SELECT id FROM cases WHERE is_present=1" + (" AND id=?" if only_case_id else ""), ((only_case_id,) if only_case_id else ()))
    for row in rows:
        integrate_case(db, row["id"], provider, status_now())


def _recompute_all(db: Database, only_case_id: int | None = None) -> None:
    with db.session() as conn:
        rows = conn.execute("SELECT id FROM cases WHERE is_present=1" + (" AND id=?" if only_case_id else ""), ((only_case_id,) if only_case_id else ())).fetchall()
        for row in rows:
            recompute_case(conn, row["id"])


def _safe_path(root: Path, path: Path) -> Path | None:
    try:
        root = root.resolve()
        candidate = path.resolve()
        candidate.relative_to(root)
        return candidate
    except (OSError, ValueError):
        return None


def _open_local(path: Path) -> None:
    if os.name == "nt":
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError:
            pass

