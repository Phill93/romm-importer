"""Small, schema-aware client for RomM's documented task API."""

import time
from typing import Any

import httpx

from ..config import Settings


class RommError(RuntimeError):
    pass


class RommClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.romm_url.rstrip("/")
        self.timeout = settings.romm_request_timeout_seconds
        self.token = settings.romm_api_token.get_secret_value()

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise RommError("RomM API token is not configured")
        return {"Authorization": f"Bearer {self.token}"}

    def check_connection(self) -> str:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                spec = client.get(f"{self.base_url}/openapi.json").json()
                if "/api/tasks/scan" not in spec.get("paths", {}):
                    raise RommError("installed RomM does not expose /api/tasks/scan")
                response = client.get(
                    f"{self.base_url}/api/tasks", headers=self._headers()
                )
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            raise RommError(f"RomM connection check failed: {exc}") from exc
        return str(spec.get("info", {}).get("version", "unknown"))

    def start_scan(self, platform_slugs: list[str]) -> str:
        payload: dict[str, Any] = {
            "type": "quick",
            "platform_fs_slugs": sorted(set(platform_slugs)),
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/api/tasks/scan",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                task_id = response.json().get("task_id")
        except (httpx.HTTPError, ValueError) as exc:
            raise RommError(f"RomM scan could not be queued: {exc}") from exc
        if not isinstance(task_id, str) or not task_id:
            raise RommError("RomM scan response did not contain a task id")
        return task_id

    def wait_for_scan(self, task_id: str, timeout_seconds: int) -> str:
        deadline = time.monotonic() + timeout_seconds
        delay = 1.0
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(
                        f"{self.base_url}/api/tasks/{task_id}",
                        headers=self._headers(),
                    )
                    response.raise_for_status()
                    status = str(response.json().get("status", "")).casefold()
            except (httpx.HTTPError, ValueError) as exc:
                raise RommError(f"RomM scan status lookup failed: {exc}") from exc
            if status in {"completed", "success", "finished"}:
                return status
            if status in {"failed", "failure", "error", "cancelled"}:
                raise RommError(f"RomM scan task ended with status {status}")
            time.sleep(delay)
            delay = min(delay * 2, 15)
        raise RommError("RomM scan status timed out")
