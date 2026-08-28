from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.release import health_release_fields

from .config import Settings
from .db import Database
from .routes import dashboard_context, register_routes


PACKAGE_DIR = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_file()
    cfg.validate()
    db = Database(cfg.database_path)
    db.initialize()
    app = FastAPI(title="Hồ sơ Digitization Manager", docs_url=None, redoc_url=None)
    app.state.settings = cfg
    app.state.db = db
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

    @app.middleware("http")
    async def csrf_cookie(request: Request, call_next):
        response = await call_next(request)
        if "csrf_token" not in request.cookies:
            response.set_cookie("csrf_token", secrets.token_urlsafe(24), httponly=False, samesite="strict")
        return response

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "hoso-digitization-manager", "offline": True, **health_release_fields()}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(request=request, name="dashboard.html", context={"settings": cfg, **dashboard_context(db)})

    @app.get("/settings")
    def get_settings(request: Request):
        if request.query_params.get("format") == "json" or "application/json" in request.headers.get("accept", ""):
            return cfg.as_dict()
        return templates.TemplateResponse(request=request, name="settings.html", context={"settings": cfg, "taxonomy_path": "document_types.json"})

    @app.post("/settings")
    async def update_settings(request: Request):
        if not _csrf_valid(request):
            return JSONResponse({"detail": "CSRF token không hợp lệ"}, status_code=403)
        payload = await _payload(request)
        if "data_root" in payload:
            cfg.data_root = Path(str(payload["data_root"])).resolve()
        if "open_browser_on_start" in payload:
            cfg.open_browser_on_start = str(payload["open_browser_on_start"]).lower() in {"1", "true", "on", "yes"}
        cfg.validate()
        cfg.save()
        return cfg.as_dict()

    register_routes(app, cfg, db, templates)
    return app


def _csrf_valid(request: Request) -> bool:
    expected = request.cookies.get("csrf_token")
    supplied = request.headers.get("x-csrf-token")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


async def _payload(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    if request.headers.get("content-type", "").startswith("application/json"):
        import json

        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body phải là object")
        return {str(k): v for k, v in value.items()}
    # Deliberately local form decoding: the existing runtime has a source
    # guard against importing network-related modules such as urllib.
    def decode(value: str) -> str:
        value = value.replace("+", " ")
        out = bytearray()
        i = 0
        while i < len(value):
            if value[i] == "%" and i + 2 < len(value):
                try:
                    out.append(int(value[i + 1 : i + 3], 16))
                    i += 3
                    continue
                except ValueError:
                    pass
            out.extend(value[i].encode("utf-8"))
            i += 1
        return out.decode("utf-8", errors="replace")

    result: dict[str, str] = {}
    for pair in raw.decode("utf-8").split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        result[decode(key)] = decode(value)
    return result


app = create_app()
