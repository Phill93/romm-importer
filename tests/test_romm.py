import pytest
from pydantic import SecretStr

from romm_importer.services.romm import RommClient, RommError


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def json(self) -> dict[str, object]:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class _Client:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def __init__(self, *, timeout: int) -> None:
        assert timeout == 20

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("get", url, None))
        if url.endswith("/openapi.json"):
            return _Response(
                {"paths": {"/api/tasks/scan": {}}, "info": {"version": "9"}}
            )
        if url.endswith("task-1"):
            return _Response({"status": "completed"})
        return _Response({"items": []})

    def post(self, url: str, **kwargs: object) -> _Response:
        payload = kwargs.get("json")
        assert isinstance(payload, dict)
        self.calls.append(("post", url, payload))
        return _Response({"task_id": "task-1"})


def test_client_checks_schema_queues_targeted_scan_and_polls(
    settings, monkeypatch
) -> None:
    settings.romm_api_token = SecretStr("token")
    _Client.calls = []
    monkeypatch.setattr("romm_importer.services.romm.httpx.Client", _Client)
    client = RommClient(settings)
    assert client.check_connection() == "9"
    assert client.start_scan(["psx", "gb", "psx"]) == "task-1"
    assert client.wait_for_scan("task-1", 5) == "completed"
    assert _Client.calls[2] == (
        "post",
        f"{settings.romm_url}/api/tasks/scan",
        {"type": "quick", "platform_fs_slugs": ["gb", "psx"]},
    )


def test_client_rejects_missing_token(settings) -> None:
    settings.romm_api_token = SecretStr("")
    with pytest.raises(RommError, match="token"):
        RommClient(settings).start_scan(["psx"])
