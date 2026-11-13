"""HTTP application for the Phase 1 MVP."""

import asyncio
import csv
import io
import shutil
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .db import build_engine, make_session_factory
from .models import (
    DatSnapshot,
    InventoryFile,
    Job,
    JobLog,
    JobStatus,
    PlatformRule,
    PublicationEntry,
)
from .schema import migrate
from .services.dats import store_snapshot
from .services.inventory import (
    InventoryWorker,
    available_sources,
    resolve_source,
    seed_rules,
)

PACKAGE_ROOT = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = build_engine(settings.database_path)
    factory = make_session_factory(engine)
    worker = InventoryWorker(factory, settings)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inventory")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings.validate_runtime()
        migrate(engine)
        with factory.begin() as session:
            seed_rules(session)
            session.execute(
                update(Job)
                .where(
                    Job.status.in_(
                        [
                            JobStatus.INVENTORYING,
                            JobStatus.ANALYZING,
                            JobStatus.PUBLISHING,
                            JobStatus.SCANNING,
                        ]
                    )
                )
                .values(
                    status=JobStatus.INTERRUPTED,
                    finished_at=datetime.now(UTC),
                    error_detail="Container restarted; job was not resumed automatically",
                )
            )
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="RomM Importer", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        https_only=settings.cookie_secure,
        same_site="strict",
        session_cookie="romm_importer_session",
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
    (
        app.state.factory,
        app.state.worker,
        app.state.executor,
        app.state.settings,
    ) = (
        factory,
        worker,
        executor,
        settings,
    )

    def publisher_factory() -> Any:
        from .services.publishing import PublishingWorker

        return PublishingWorker(factory, settings)

    def romm_client_factory() -> Any:
        from .services.romm import RommClient

        return RommClient(settings)

    app.state.publisher_factory = publisher_factory
    app.state.romm_client_factory = romm_client_factory

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        """Send browser navigations to the login form while preserving API semantics."""
        accepts_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code == 401 and accepts_html:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    def db_session() -> Any:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    def admin(request: Request) -> None:
        if request.session.get("admin") != settings.admin_username:
            raise HTTPException(status_code=401, detail="authentication required")

    async def csrf(request: Request) -> None:
        supplied = request.headers.get("x-csrf-token")
        if not supplied:
            form_token = (await request.form()).get("csrf_token")
            supplied = form_token if isinstance(form_token, str) else None
        if not supplied or not compare_digest(
            str(supplied), request.session.get("csrf", "")
        ):
            raise HTTPException(status_code=403, detail="invalid CSRF token")

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "request": request,
            "csrf_token": request.session.get("csrf", ""),
            **extra,
        }

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "login.html", context(request))

    @app.post("/login")
    def login(
        request: Request, username: str = Form(), password: str = Form()
    ) -> Response:
        try:
            valid = compare_digest(
                username, settings.admin_username
            ) and PasswordHasher().verify(
                settings.admin_password_hash.get_secret_value(), password
            )
        except VerificationError:
            valid = False
        if not valid:
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error="Ungültige Anmeldedaten"),
                status_code=401,
            )
        request.session.clear()
        request.session.update(
            {"admin": settings.admin_username, "csrf": token_urlsafe(32)}
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(
        request: Request, _: None = Depends(admin), __: None = Depends(csrf)
    ) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        _: None = Depends(admin),
        session: Session = Depends(db_session),
    ) -> Response:
        jobs = list(
            session.scalars(select(Job).order_by(Job.created_at.desc()).limit(10))
        )
        counts: dict[JobStatus, int] = {}
        for row in session.execute(
            select(Job.status, func.count()).group_by(Job.status)
        ):
            counts[row[0]] = int(row[1])
        try:
            library_usage = shutil.disk_usage(settings.library_roms_root)
        except OSError:
            library_usage = None
        error_count = int(
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status == JobStatus.FAILED)
            )
            or 0
        )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(
                request,
                jobs=jobs,
                counts=counts,
                library_usage=library_usage,
                error_count=error_count,
            ),
        )

    @app.get("/incoming", response_class=HTMLResponse)
    def incoming(request: Request, _: None = Depends(admin)) -> Response:
        with factory() as session:
            snapshots = list(
                session.scalars(
                    select(DatSnapshot).order_by(DatSnapshot.imported_at.desc())
                )
            )
        return templates.TemplateResponse(
            request,
            "incoming.html",
            context(
                request,
                sources=available_sources(settings.incoming_root),
                snapshots=snapshots,
            ),
        )

    @app.post("/jobs")
    def create_job(
        request: Request,
        name: str = Form(),
        source_key: str = Form(),
        dat_snapshot_id: str = Form(default=""),
        _: None = Depends(admin),
        __: None = Depends(csrf),
        session: Session = Depends(db_session),
    ) -> Response:
        try:
            resolve_source(settings.incoming_root, source_key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        clean_name = name.strip()[:160]
        if not clean_name:
            raise HTTPException(422, "job name is required")
        try:
            selected_snapshot_id = (
                int(dat_snapshot_id) if dat_snapshot_id.strip() else None
            )
        except ValueError as exc:
            raise HTTPException(422, "invalid DAT snapshot") from exc
        if selected_snapshot_id is not None and not session.get(
            DatSnapshot, selected_snapshot_id
        ):
            raise HTTPException(422, "DAT snapshot not found")
        job = Job(
            id=str(uuid4()),
            name=clean_name,
            source_key=source_key,
            dat_snapshot_id=selected_snapshot_id,
        )
        session.add(job)
        session.commit()
        executor.submit(worker.run, job.id)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(
        request: Request,
        page: int = 1,
        status: str = "",
        _: None = Depends(admin),
        session: Session = Depends(db_session),
    ) -> Response:
        page = max(page, 1)
        statement = select(Job).order_by(Job.created_at.desc())
        if status:
            try:
                statement = statement.where(Job.status == JobStatus(status))
            except ValueError:
                raise HTTPException(422, "invalid job status") from None
        statement = statement.offset((page - 1) * settings.page_size_default).limit(
            settings.page_size_default
        )
        return templates.TemplateResponse(
            request,
            "jobs.html",
            context(
                request,
                jobs=list(session.scalars(statement)),
                page=page,
                selected_status=status,
                statuses=list(JobStatus),
            ),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(
        job_id: str,
        request: Request,
        _: None = Depends(admin),
        session: Session = Depends(db_session),
    ) -> Response:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        entries = list(
            session.scalars(
                select(PublicationEntry)
                .where(PublicationEntry.job_id == job_id)
                .order_by(PublicationEntry.target_path)
            )
        )
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            context(request, job=job, publication_entries=entries),
        )

    @app.post("/jobs/{job_id}/approve")
    def approve_job(
        job_id: str,
        request: Request,
        _: None = Depends(admin),
        __: None = Depends(csrf),
        session: Session = Depends(db_session),
    ) -> Response:
        job = session.get(Job, job_id)
        if not job or job.status != JobStatus.AWAITING_APPROVAL:
            raise HTTPException(409, "job is not awaiting approval")
        try:
            publisher = app.state.publisher_factory()
            publisher.plan(job_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        executor.submit(publisher.run, job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/retry-publish")
    def retry_publish(
        job_id: str,
        request: Request,
        _: None = Depends(admin),
        __: None = Depends(csrf),
        session: Session = Depends(db_session),
    ) -> Response:
        job = session.get(Job, job_id)
        if not job or job.status != JobStatus.INTERRUPTED:
            raise HTTPException(409, "job publication cannot be resumed")
        publisher = app.state.publisher_factory()
        executor.submit(publisher.run, job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/retry-scan")
    def retry_scan(
        job_id: str,
        request: Request,
        _: None = Depends(admin),
        __: None = Depends(csrf),
        session: Session = Depends(db_session),
    ) -> Response:
        job = session.get(Job, job_id)
        if not job or not job.publication_completed_at:
            raise HTTPException(409, "job has no completed publication")
        publisher = app.state.publisher_factory()
        executor.submit(publisher.run, job_id, True)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: str,
        request: Request,
        _: None = Depends(admin),
        __: None = Depends(csrf),
        session: Session = Depends(db_session),
    ) -> Response:
        job = session.get(Job, job_id)
        if not job or job.status not in {
            JobStatus.CREATED,
            JobStatus.INVENTORYING,
            JobStatus.ANALYZING,
        }:
            raise HTTPException(409, "job cannot be cancelled")
        job.cancel_requested = True
        session.commit()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}/files", response_class=HTMLResponse)
    def job_files(
        job_id: str,
        request: Request,
        page: int = 1,
        _: None = Depends(admin),
        session: Session = Depends(db_session),
    ) -> Response:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        page = max(page, 1)
        files = list(
            session.scalars(
                select(InventoryFile)
                .where(InventoryFile.job_id == job_id)
                .order_by(InventoryFile.relative_path)
                .offset((page - 1) * settings.page_size_default)
                .limit(settings.page_size_default)
            )
        )
        return templates.TemplateResponse(
            request,
            "files.html",
            context(
                request,
                job=job,
                files=files,
                page=page,
                page_size=settings.page_size_default,
            ),
        )

    @app.get("/jobs/{job_id}/report.csv")
    def csv_report(
        job_id: str, _: None = Depends(admin), session: Session = Depends(db_session)
    ) -> Response:
        if not session.get(Job, job_id):
            raise HTTPException(404, "job not found")
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "path",
                "size_bytes",
                "sha256",
                "platform",
                "dat_status",
                "action",
                "error",
            ]
        )
        for item in session.scalars(
            select(InventoryFile)
            .where(InventoryFile.job_id == job_id)
            .order_by(InventoryFile.relative_path)
        ):
            writer.writerow(
                [
                    item.relative_path,
                    item.size_bytes,
                    item.sha256,
                    item.platform,
                    item.dat_status,
                    item.proposed_action,
                    item.error_detail,
                ]
            )
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.csv"'},
        )

    @app.get("/jobs/{job_id}/events")
    async def events(
        job_id: str, request: Request, _: None = Depends(admin)
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last_id = 0
            while not await request.is_disconnected():
                with factory() as session:
                    logs = list(
                        session.scalars(
                            select(JobLog)
                            .where(JobLog.job_id == job_id, JobLog.id > last_id)
                            .order_by(JobLog.id)
                        )
                    )
                    job = session.get(Job, job_id)
                for log in logs:
                    last_id = log.id
                    yield f"event: log\ndata: {log.id}|{log.level}|{log.message}\n\n"
                if job and job.status in {
                    JobStatus.AWAITING_APPROVAL,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.INTERRUPTED,
                    JobStatus.COMPLETED,
                }:
                    yield f"event: status\ndata: {job.status}\n\n"
                    return
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/settings", response_class=HTMLResponse)
    def rules(
        request: Request,
        _: None = Depends(admin),
        session: Session = Depends(db_session),
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "settings.html",
            context(
                request,
                rules=list(
                    session.scalars(
                        select(PlatformRule).order_by(PlatformRule.source_name)
                    )
                ),
                snapshots=list(
                    session.scalars(
                        select(DatSnapshot).order_by(DatSnapshot.imported_at.desc())
                    )
                ),
                romm_url=settings.romm_url,
            ),
        )

    @app.post("/settings/dats")
    async def upload_dat(
        request: Request,
        source_label: str = Form(),
        source_url: str = Form(default=""),
        snapshot: UploadFile = File(),
        _: None = Depends(admin),
        __: None = Depends(csrf),
        session: Session = Depends(db_session),
    ) -> Response:
        stored = await store_snapshot(
            session, settings, snapshot, source_label, source_url or None
        )
        session.commit()
        return RedirectResponse(f"/settings?dat={stored.id}", status_code=303)

    @app.post("/settings/rules/{rule_id}")
    def update_rule(
        rule_id: int,
        request: Request,
        source_name: str = Form(),
        romm_directory: str = Form(),
        extensions: str = Form(),
        target_format: str = Form(),
        converter: str = Form(),
        _: None = Depends(admin),
        __: None = Depends(csrf),
        session: Session = Depends(db_session),
    ) -> Response:
        rule = session.get(PlatformRule, rule_id)
        values = [source_name, romm_directory, target_format, converter]
        suffixes = [part.strip() for part in extensions.split(",") if part.strip()]
        if (
            not rule
            or any(not value.strip() for value in values)
            or any(not suffix.startswith(".") or "/" in suffix for suffix in suffixes)
        ):
            raise HTTPException(422, "invalid platform rule")
        rule.source_name, rule.romm_directory = (
            source_name.strip(),
            romm_directory.strip(),
        )
        rule.extensions, rule.target_format, rule.converter = (
            ",".join(suffixes),
            target_format.strip(),
            converter.strip(),
        )
        session.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/romm/test")
    def test_romm(
        request: Request, _: None = Depends(admin), __: None = Depends(csrf)
    ) -> Response:
        try:
            version = app.state.romm_client_factory().check_connection()
            return RedirectResponse(f"/settings?romm=ok-{version}", status_code=303)
        except Exception as exc:
            return RedirectResponse(
                f"/settings?romm_error={str(exc)[:180]}", status_code=303
            )

    return app


app = create_app()
