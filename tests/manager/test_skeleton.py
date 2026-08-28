from pathlib import Path

from fastapi.testclient import TestClient

from app.manager.config import Settings
from app.manager.main import create_app
from app.release import APP_VERSION, read_provenance


def test_health_and_dashboard(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "input", database_path=tmp_path / "data" / "app.db")
    app = create_app(settings)
    client = TestClient(app)
    health = client.get("/health").json()
    assert health["offline"] is True
    assert health["version"] == APP_VERSION
    assert health["build_sha"] == "unpackaged"
    response = client.get("/")
    assert response.status_code == 200
    assert "Hồ sơ Digitization Manager" in response.text


def test_settings_post_requires_csrf(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "input", database_path=tmp_path / "data" / "app.db")
    client = TestClient(create_app(settings))
    assert client.post("/settings", json={"data_root": str(tmp_path)}).status_code == 403


def test_packaged_provenance_requires_current_version_and_full_sha(tmp_path: Path):
    provenance = tmp_path / "build_provenance.json"
    provenance.write_text('{"version":"0.2.1-rc1","build_sha":"' + "a" * 40 + '"}', encoding="utf-8")
    assert read_provenance(provenance)["build_sha"] == "a" * 40
    provenance.write_text('{"version":"0.2.0","build_sha":"' + "a" * 40 + '"}', encoding="utf-8")
    assert read_provenance(provenance)["build_sha"] == "unpackaged"