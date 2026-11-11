"""Validated runtime configuration."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_path: Path = Path("/appdata/importer.sqlite3")
    incoming_root: Path = Path("/data/incoming")
    processing_root: Path = Path("/data/processing")
    quarantine_root: Path = Path("/data/quarantine")
    reports_root: Path = Path("/data/reports")
    dats_root: Path = Path("/data/dats")
    library_roms_root: Path = Path("/data/library/roms")
    library_bios_root: Path = Path("/data/library/bios")
    admin_username: str = "admin"
    admin_password_hash: SecretStr = Field(default=SecretStr(""))
    session_secret: SecretStr = Field(default=SecretStr(""))
    cookie_secure: bool = True
    romm_url: str = "http://romm:8080"
    romm_api_token: SecretStr = Field(default=SecretStr(""))
    max_inventory_file_bytes: int = 64 * 1024 * 1024 * 1024
    max_dat_upload_bytes: int = 2 * 1024 * 1024 * 1024
    process_timeout_seconds: int = 6 * 60 * 60
    process_kill_grace_seconds: int = 15
    igir_max_old_space_mb: int = Field(default=1400, ge=256, le=1536)
    romm_request_timeout_seconds: int = Field(default=20, ge=2, le=120)
    romm_scan_timeout_seconds: int = Field(default=1800, ge=30, le=21600)
    max_archive_entries: int = Field(default=5000, ge=1, le=50000)
    max_archive_file_bytes: int = Field(default=4 * 1024 * 1024 * 1024, ge=1)
    max_archive_total_bytes: int = Field(default=8 * 1024 * 1024 * 1024, ge=1)
    page_size_default: int = 50
    page_size_max: int = 200

    @field_validator("admin_username")
    @classmethod
    def username_is_safe(cls, value: str) -> str:
        if not value or len(value) > 64:
            raise ValueError("ADMIN_USERNAME must contain 1-64 characters")
        return value

    def validate_runtime(self) -> None:
        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        if not self.admin_password_hash.get_secret_value().startswith("$argon2"):
            raise ValueError("ADMIN_PASSWORD_HASH must be an Argon2 hash")
        if not self.incoming_root.is_dir():
            raise ValueError(f"incoming root does not exist: {self.incoming_root}")


def get_settings() -> Settings:
    return Settings()
