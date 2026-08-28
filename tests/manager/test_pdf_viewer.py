from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.manager.config import Settings
from app.manager.main import create_app


def _pdf(path: Path) -> None:
    writer = PdfWriter(); writer.add_blank_page(width=200, height=300)
    with path.open("wb") as handle: writer.write(handle)


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    root = tmp_path / "done" / "output"; folder = root / "SYNTHETIC_PERSON_A"; folder.mkdir(parents=True)
    _pdf(folder / "05.Quyet_dinh_ket_nap_dang_vien.pdf")
    cfg = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    app = create_app(cfg); client = TestClient(app)
    token = client.get("/").cookies.get("csrf_token")
    assert token
    assert client.post("/scan", headers={"x-csrf-token": token}).status_code == 200
    return client, folder


def test_viewer_streams_pdf_inline_without_mutation(tmp_path: Path):
    client, folder = _client(tmp_path)
    source = folder / "05.Quyet_dinh_ket_nap_dang_vien.pdf"; before = hashlib.sha256(source.read_bytes()).hexdigest()
    detail = client.get("/cases/1?format=json").json(); document_id = detail["documents"][0]["id"]
    response = client.get(f"/documents/{document_id}/view")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["cache-control"] == "private, no-store"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_viewer_rejects_unknown_missing_and_outside_paths(tmp_path: Path):
    client, folder = _client(tmp_path)
    assert client.get("/documents/999/view").status_code == 404
    detail = client.get("/cases/1?format=json").json(); document_id = detail["documents"][0]["id"]
    (folder / "05.Quyet_dinh_ket_nap_dang_vien.pdf").unlink()
    assert client.get(f"/documents/{document_id}/view").status_code == 404
    db = client.app.state.db
    with db.session() as conn:
        conn.execute("UPDATE documents SET relative_path=? WHERE id=?", ("../../outside.pdf", document_id))
    assert client.get(f"/documents/{document_id}/view").status_code == 404