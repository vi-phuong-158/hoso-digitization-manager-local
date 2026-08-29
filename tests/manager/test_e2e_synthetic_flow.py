from __future__ import annotations

import io
from pathlib import Path
from PIL import Image
from pypdf import PdfReader, PdfWriter
from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.main import create_app
from tests.manager.fixtures import build_fixture_tree


def _make_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    with path.open("wb") as handle:
        writer.write(handle)


def test_full_user_flow_synthetic_e2e(tmp_path: Path):
    data_root = tmp_path / "done" / "output"
    fixtures = build_fixture_tree(data_root)
    db_path = tmp_path / "data" / "manager.db"
    cfg = Settings(data_root=data_root, database_path=db_path)
    app = create_app(cfg)
    client = TestClient(app)

    # 1. Dashboard entry & CSRF
    dash_resp = client.get("/")
    assert dash_resp.status_code == 200
    token = client.cookies.get("csrf_token")
    assert token

    # 2. Scan data
    scan_resp = client.post("/scan", headers={"X-CSRF-Token": token})
    assert scan_resp.status_code == 200
    assert scan_resp.json()["folders_seen"] == 10

    # 3. Case list
    cases_resp = client.get("/cases?format=json")
    assert cases_resp.status_code == 200
    items = cases_resp.json()["items"]
    assert len(items) == 10
    first_case = items[0]
    case_id = first_case["id"]

    # 4. Case detail
    detail_resp = client.get(f"/cases/{case_id}")
    assert detail_resp.status_code == 200
    assert "Checklist 104 loại tài liệu" in detail_resp.text

    # 5. Manual Add Document Flow
    img1 = Image.new("RGB", (200, 300), color=(255, 255, 255))
    img1_io = io.BytesIO()
    img1.save(img1_io, format="JPEG")
    img1_bytes = img1_io.getvalue()

    img2 = Image.new("RGB", (200, 300), color=(240, 240, 240))
    img2_io = io.BytesIO()
    img2.save(img2_io, format="PNG")
    img2_bytes = img2_io.getvalue()

    files = [
        ("files", ("page1.jpg", img1_bytes, "image/jpeg")),
        ("files", ("page2.png", img2_bytes, "image/png")),
    ]
    prep_resp = client.post("/manual-documents/prepare", data={"case_id": case_id}, files=files, headers={"X-CSRF-Token": token})
    assert prep_resp.status_code == 200
    prep_data = prep_resp.json()
    upload_token = prep_data["token"]
    pages = prep_data["pages"]
    assert len(pages) == 2

    # Test name preview for 86
    name_resp = client.get(f"/manual-documents/name-preview?case_id={case_id}&type_id=86")
    assert name_resp.status_code == 200
    expected_filename = name_resp.json()["filename"]
    assert expected_filename.startswith("86.")

    # Save document with reordering & rotation
    save_payload = {
        "case_id": case_id,
        "token": upload_token,
        "type_id": "86",
        "pages": [
            {"id": pages[1]["id"], "rotation": 90},
            {"id": pages[0]["id"], "rotation": 0},
        ],
        "document_date": "2024-05-19",
        "note": "Bằng cử nhân"
    }
    save_resp = client.post("/manual-documents/save", json=save_payload, headers={"X-CSRF-Token": token})
    assert save_resp.status_code == 200
    saved_filename = save_resp.json()["filename"]
    assert saved_filename == expected_filename

    # Verify saved PDF on disk
    target_pdf = Path(first_case["folder_path"]) / saved_filename
    assert target_pdf.is_file()
    reader = PdfReader(str(target_pdf))
    assert len(reader.pages) == 2

    # 6. PDF Inline Viewer
    detail_updated = client.get(f"/cases/{case_id}?format=json").json()
    doc_id = next(d["id"] for d in detail_updated["documents"] if d["filename"] == saved_filename)
    view_resp = client.get(f"/documents/{doc_id}/view")
    assert view_resp.status_code == 200
    assert view_resp.headers["content-type"].startswith("application/pdf")
    assert view_resp.headers["content-disposition"] == "inline"

    # 7. Backup & Settings
    # Backup is a state-changing action (it writes a new file to disk), so it
    # must go through POST + CSRF like every other mutating route - a plain
    # GET must never have that side effect.
    backup_resp = client.post("/backup", headers={"X-CSRF-Token": token})
    assert backup_resp.status_code == 200
    assert Path(backup_resp.json()["path"]).is_file()

    settings_resp = client.post("/settings", json={"data_root": str(data_root)}, headers={"X-CSRF-Token": token})
    assert settings_resp.status_code == 200
