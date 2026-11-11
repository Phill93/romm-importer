import pytest

from romm_importer.config import Settings


def test_invalid_runtime_secrets_are_rejected(tmp_path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    settings = Settings(incoming_root=incoming, session_secret="short")
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        settings.validate_runtime()
