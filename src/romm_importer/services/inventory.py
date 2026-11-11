"""Safe, read-only inventory processing."""

import hashlib
import os
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import InventoryFile, Job, JobLog, JobStatus, PlatformRule
from ..state import transition_allowed
from .archives import prepare_source

DEFAULT_RULES = (
    ("gb", "gb", ".gb", "original", "none"),
    ("gbc", "gbc", ".gbc", "original", "none"),
    ("gba", "gba", ".gba", "original", "none"),
    ("snes", "snes", ".sfc,.smc", "original", "none"),
    ("n64", "n64", ".n64,.z64,.v64", "original", "none"),
    ("nds", "nds", ".nds", "original", "none"),
    ("gc", "ngc", ".iso,.gcm", "original", "none"),
    ("psx", "psx", ".cue,.bin,.iso", "chd", "chd_cd"),
    ("ps2", "ps2", ".iso,.bin,.cue", "chd", "chd_auto"),
    ("psp", "psp", ".iso,.cso", "original", "none"),
)


def seed_rules(session: Session) -> None:
    if session.scalar(select(PlatformRule.id).limit(1)):
        return
    session.add_all(
        PlatformRule(
            source_name=a,
            romm_directory=b,
            extensions=c,
            target_format=d,
            converter=e,
        )
        for a, b, c, d, e in DEFAULT_RULES
    )


def resolve_source(root: Path, source_key: str) -> Path:
    """Resolve a source key without allowing traversal or symlink escape."""
    if source_key == ".":
        candidate = root
    else:
        if Path(source_key).is_absolute() or ".." in Path(source_key).parts:
            raise ValueError("invalid source path")
        candidate = root / source_key
    root_real = root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or not resolved.is_relative_to(root_real):
        raise ValueError("source is outside the incoming root")
    return resolved


def available_sources(root: Path) -> list[dict[str, str]]:
    result = [{"key": ".", "name": "incoming"}]
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            result.append({"key": entry.name, "name": entry.name})
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def infer_platform(
    relative_path: Path, rules: list[PlatformRule]
) -> tuple[str | None, str]:
    parts = {part.casefold() for part in relative_path.parts[:-1]}
    extension = relative_path.suffix.casefold()
    for rule in rules:
        extensions = {value.strip() for value in rule.extensions.split(",")}
        if rule.source_name.casefold() in parts or extension in extensions:
            return rule.romm_directory, rule.target_format
    return None, "review"


class InventoryWorker:
    """Single-process job worker. It never writes into incoming or library paths."""

    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory = factory
        self.settings = settings

    def log(self, session: Session, job_id: str, level: str, message: str) -> None:
        session.add(JobLog(job_id=job_id, level=level, message=message))

    def request_cancel(self, job_id: str) -> None:
        with self.factory.begin() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.cancel_requested = True
                self.log(session, job_id, "info", "Cancellation requested")

    def run(self, job_id: str) -> None:
        try:
            analyze_with_dat = False
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job is None or not transition_allowed(
                    job.status, JobStatus.INVENTORYING
                ):
                    return
                job.status = JobStatus.INVENTORYING
                job.started_at = datetime.now(UTC)
                self.log(session, job_id, "info", "Inventory started")
                source = resolve_source(self.settings.incoming_root, job.source_key)
                source = prepare_source(
                    source,
                    self.settings.processing_root / "jobs" / job_id / "input",
                    self.settings,
                )
                rules = list(session.scalars(select(PlatformRule)).all())
            platform_counts: Counter[str] = Counter()
            total_files = total_bytes = successful = failed = 0
            for directory, names, _files in os.walk(source, followlinks=False):
                directory_path = Path(directory)
                names[:] = [
                    name for name in names if not (directory_path / name).is_symlink()
                ]
                for name in _files:
                    path = directory_path / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative = path.relative_to(source)
                    with self.factory.begin() as session:
                        job = session.get(Job, job_id)
                        if job is None or job.cancel_requested:
                            self._cancel(session, job_id)
                            return
                    try:
                        size = path.stat().st_size
                        if size > self.settings.max_inventory_file_bytes:
                            raise ValueError("file exceeds configured inventory limit")
                        checksum = sha256_file(path)
                        platform, action = infer_platform(relative, rules)
                        if platform:
                            platform_counts[platform] += 1
                        record = InventoryFile(
                            job_id=job_id,
                            relative_path=str(relative),
                            size_bytes=size,
                            extension=path.suffix.casefold(),
                            sha256=checksum,
                            platform=platform,
                            game_guess=path.stem,
                            proposed_action=action,
                        )
                        total_files += 1
                        total_bytes += size
                        successful += 1
                    except (OSError, ValueError) as exc:
                        record = InventoryFile(
                            job_id=job_id,
                            relative_path=str(relative),
                            size_bytes=0,
                            extension=path.suffix.casefold(),
                            dat_status="error",
                            proposed_action="review",
                            error_detail=str(exc),
                        )
                        failed += 1
                    with self.factory.begin() as session:
                        session.add(record)
                        if (total_files + failed) % 20 == 0:
                            self.log(
                                session,
                                job_id,
                                "info",
                                f"Inventoried {total_files + failed} files",
                            )
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job is None or job.cancel_requested:
                    self._cancel(session, job_id)
                    return
                job.total_files, job.total_bytes = total_files, total_bytes
                job.successful_files, job.failed_files = successful, failed
                job.detected_platforms = dict(platform_counts)
                analyze_with_dat = job.dat_snapshot_id is not None
                if not analyze_with_dat:
                    job.status = JobStatus.AWAITING_APPROVAL
                    job.finished_at = datetime.now(UTC)
                    self.log(session, job_id, "info", "Inventory ready for review")
                else:
                    job.status = JobStatus.ANALYZING
                    self.log(session, job_id, "info", "Starting Igir DAT analysis")
            if analyze_with_dat:
                from .processing import ProcessingWorker

                ProcessingWorker(self.factory, self.settings).run(job_id)
        except Exception as exc:
            work = self.settings.processing_root / "jobs" / job_id
            quarantine = self.settings.quarantine_root / job_id
            if work.exists() and not quarantine.exists():
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(work), str(quarantine))
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job is not None:
                    job.status, job.error_detail, job.finished_at = (
                        JobStatus.FAILED,
                        str(exc),
                        datetime.now(UTC),
                    )
                    self.log(session, job_id, "error", "Inventory failed")

    def _cancel(self, session: Session, job_id: str) -> None:
        job = session.get(Job, job_id)
        if job is not None:
            job.status, job.finished_at = JobStatus.CANCELLED, datetime.now(UTC)
            self.log(session, job_id, "info", "Job cancelled")
