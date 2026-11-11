from datetime import datetime

from fastapi.testclient import TestClient

from romm_importer.models import Job, JobStatus


def csrf_token(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookie = response.cookies.get("romm_importer_session")
    assert cookie
    # The signed cookie is intentionally opaque; fetch a rendered authenticated page instead.
    page = client.get("/incoming")
    assert page.status_code == 200
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def test_login_required(client: TestClient) -> None:
    assert client.get("/").status_code == 401


def test_browser_navigation_redirects_to_login(client: TestClient) -> None:
    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_and_csrf_protected_creation(client: TestClient) -> None:
    token = csrf_token(client)
    denied = client.post("/jobs", data={"name": "one", "source_key": "."})
    assert denied.status_code == 403
    created = client.post(
        "/jobs",
        data={"name": "one", "source_key": ".", "csrf_token": token},
        follow_redirects=False,
    )
    assert created.status_code == 303
    job_url = created.headers["location"]
    assert client.get("/").status_code == 200
    assert client.get("/jobs").status_code == 200
    assert client.get("/settings").status_code == 200
    assert client.get(job_url).status_code == 200
    assert client.get(f"{job_url}/files").status_code == 200
    report = client.get(f"{job_url}/report.csv")
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/csv")


def test_invalid_login_is_rejected(client: TestClient) -> None:
    assert (
        client.post("/login", data={"username": "admin", "password": "no"}).status_code
        == 401
    )


def test_unknown_job_is_not_found(client: TestClient) -> None:
    csrf_token(client)
    assert client.get("/jobs/not-a-job").status_code == 404
    assert client.get("/jobs/not-a-job/files").status_code == 404
    assert client.get("/jobs/not-a-job/report.csv").status_code == 404


def test_approval_and_scan_retry_are_csrf_protected(client: TestClient) -> None:
    calls: list[tuple[str, bool]] = []

    class FakePublisher:
        def plan(self, job_id: str) -> list[object]:
            calls.append((f"plan:{job_id}", False))
            return []

        def run(self, job_id: str, scan_only: bool = False) -> None:
            calls.append((job_id, scan_only))

    client.app.state.publisher_factory = FakePublisher  # type: ignore[attr-defined]
    factory = client.app.state.factory  # type: ignore[attr-defined]
    with factory.begin() as session:
        session.add(
            Job(
                id="approve",
                name="approve",
                source_key=".",
                status=JobStatus.AWAITING_APPROVAL,
            )
        )
        session.add(
            Job(
                id="scan",
                name="scan",
                source_key=".",
                status=JobStatus.FAILED,
                publication_completed_at=datetime.now(),
            )
        )
    token = csrf_token(client)
    assert (
        client.post("/jobs/approve/approve", data={"csrf_token": token}).status_code
        == 200
    )
    assert (
        client.post("/jobs/scan/retry-scan", data={"csrf_token": token}).status_code
        == 200
    )
    assert ("plan:approve", False) in calls
    assert ("approve", False) in calls
    assert ("scan", True) in calls


def test_romm_connection_test_uses_configured_client(client: TestClient) -> None:
    class FakeRomm:
        def check_connection(self) -> str:
            return "5.4.0"

    client.app.state.romm_client_factory = FakeRomm  # type: ignore[attr-defined]
    token = csrf_token(client)
    response = client.post(
        "/settings/romm/test", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?romm=ok-5.4.0"
