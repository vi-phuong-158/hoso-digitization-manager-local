"""Acceptance checks for the local manual document workflow.

Kept at the repository root because the existing tests are currently archived
in this workspace; it can be moved under tests/ when the test tree is restored.
"""
from __future__ import annotations

from pathlib import Path
import hashlib

from PIL import Image
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.manager.config import Settings
from app.manager.main import create_app


def _client(tmp_path: Path):
    root = tmp_path / "done" / "output"
    (root / "Nguyễn Văn A").mkdir(parents=True)
    settings = Settings(data_root=root, database_path=tmp_path / "data" / "manager.db")
    client = TestClient(create_app(settings))
    client.get("/")
    token = client.cookies["csrf_token"]
    assert client.post("/scan", headers={"X-CSRF-Token": token}).status_code == 200
    case_id = client.get("/cases?format=json").json()["items"][0]["id"]
    return client, token, case_id, root / "Nguyễn Văn A"


def _prepare(client, token, case_id, files):
    response = client.post("/manual-documents/prepare", data={"case_id": str(case_id)}, files=files,
                           headers={"X-CSRF-Token": token})
    assert response.status_code == 200, response.text
    return response.json()


def test_manual_images_reorder_rotate_and_source_immutable(tmp_path: Path):
    client, token, case_id, folder = _client(tmp_path)
    images = []
    for name, color in (("a.jpg", "red"), ("b.png", "green"), ("c.jpg", "blue")):
        path = tmp_path / name
        Image.new("RGB", (80, 100), color).save(path)
        images.append((name, path.read_bytes(), "image/jpeg" if path.suffix == ".jpg" else "image/png"))
    before = [hashlib.sha256(value[1]).hexdigest() for value in images]
    prepared = _prepare(client, token, case_id, [("files", value) for value in images])
    pages = list(reversed(prepared["pages"]))
    pages[0]["rotation"] = 90
    saved = client.post("/manual-documents/save", json={"case_id": case_id, "token": prepared["token"],
        "type_id": "86", "pages":[{"id": p["id"], "rotation": p.get("rotation", 0)} for p in pages]},
        headers={"X-CSRF-Token": token})
    assert saved.status_code == 200, saved.text
    output = folder / "86.Cac_van_bang_chung_chi_chuyen_mon_nghiep_vu_ngoai_ngu_tin_hoc_boi_duong_chuc_danh_ngach_chung_chi_hanh_nghe_ban_sao_co_chung_thuc.pdf"
    assert output.is_file() and len(PdfReader(str(output)).pages) == 3
    assert before == [hashlib.sha256(value[1]).hexdigest() for value in images]


def test_manual_global_suffix_and_taxonomy_search(tmp_path: Path):
    client, token, case_id, folder = _client(tmp_path)
    existing = folder / "05.Quyet_dinh_ket_nap_dang_vien.pdf"
    existing.write_bytes(b"not-a-real-pdf")
    image = tmp_path / "one.jpg"
    Image.new("RGB", (40, 40), "white").save(image)
    prepared = _prepare(client, token, case_id, [("files", ("one.jpg", image.read_bytes(), "image/jpeg"))])
    response = client.post("/manual-documents/save", json={"case_id": case_id, "token": prepared["token"],
        "type_id":"05", "pages":[{"id":"p1","rotation":0}]}, headers={"X-CSRF-Token": token})
    assert response.status_code == 200, response.text
    assert (folder / "05.Quyet_dinh_ket_nap_dang_vien.1.pdf").is_file()
    assert (folder / "05.Quyet_dinh_ket_nap_dang_vien.2.pdf").is_file()
