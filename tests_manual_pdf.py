"""Smoke coverage for importing a multi-page PDF through the manual flow."""
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.manager.config import Settings
from app.manager.main import create_app


def test_manual_multipage_pdf_preserves_page_count(tmp_path: Path):
    root = tmp_path / "done" / "output"
    case_folder = root / "Nguyen Van A"
    case_folder.mkdir(parents=True)
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_blank_page(width=400, height=500)
    with source.open("wb") as handle:
        writer.write(handle)

    client = TestClient(create_app(Settings(data_root=root, database_path=tmp_path / "manager.db")))
    client.get("/")
    token = client.cookies.get("csrf_token")
    assert client.post("/scan", headers={"X-CSRF-Token": token}).status_code == 200
    case_id = client.get("/cases?format=json").json()["items"][0]["id"]
    prepared = client.post(
        "/manual-documents/prepare",
        data={"case_id": str(case_id)},
        files=[("files", ("source.pdf", source.read_bytes(), "application/pdf"))],
        headers={"X-CSRF-Token": token},
    )
    assert prepared.status_code == 200, prepared.text
    pages = prepared.json()["pages"]
    assert len(pages) == 2
    saved = client.post(
        "/manual-documents/save",
        json={"case_id": case_id, "token": prepared.json()["token"], "type_id": "05",
              "pages": [{"id": page["id"], "rotation": 0} for page in pages]},
        headers={"X-CSRF-Token": token},
    )
    assert saved.status_code == 200, saved.text
    output = case_folder / "05.Quyet_dinh_ket_nap_dang_vien.pdf"
    assert output.is_file()
    assert len(PdfReader(str(output)).pages) == 2
