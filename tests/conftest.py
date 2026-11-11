from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from romm_importer.config import Settings
from romm_importer.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    return Settings(
        database_path=tmp_path / "appdata" / "importer.sqlite3",
        incoming_root=incoming,
        processing_root=tmp_path / "processing",
        quarantine_root=tmp_path / "quarantine",
        reports_root=tmp_path / "reports",
        dats_root=tmp_path / "dats",
        library_roms_root=tmp_path / "library" / "roms",
        library_bios_root=tmp_path / "library" / "bios",
        admin_username="admin",
        admin_password_hash=PasswordHasher().hash("correct horse battery staple"),
        session_secret="x" * 48,
        cookie_secure=False,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with client.session_transaction() as session:  # type: ignore[attr-defined]
        return str(session["csrf"])
