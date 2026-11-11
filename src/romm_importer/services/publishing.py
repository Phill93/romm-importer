"""Idempotent, staged publication into the RomM library datasets."""

import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import (
    InventoryFile,
    Job,
    JobLog,
    JobStatus,
    ProcessingArtifact,
    PublicationEntry,
)
from .inventory import resolve_source, sha256_file
from .romm import RommClient, RommError

SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()\[\],+&'-]+")
DISC_MARKER = re.compile(
    r"(?:[ ._\-]*(?:disc|disk|cd)\s*(\d+))(?=\s*[\])}]|$)", re.IGNORECASE
)


def safe_name(name: str) -> str:
    cleaned = SAFE_NAME.sub("_", name).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("DAT game name cannot produce a safe filename")
    return cleaned[:220]


def disc_group_name(name: str) -> str:
    """Return a stable game name shared by Disc 1/Disc 2 variants."""
    grouped = DISC_MARKER.sub("", name)
    grouped = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", grouped).strip(" ._-")
    return safe_name(grouped or name)


class PublishingWorker:
    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory = factory
        self.settings = settings

    def _log(self, session: Session, job_id: str, level: str, message: str) -> None:
        session.add(JobLog(job_id=job_id, level=level, message=message))

    def plan(self, job_id: str) -> list[PublicationEntry]:
        """Create a durable manifest without making library files visible."""
        with self.factory.begin() as session:
            existing = list(
                session.scalars(
                    select(PublicationEntry).where(PublicationEntry.job_id == job_id)
                )
            )
            if existing:
                return existing
            job = session.get(Job, job_id)
            if not job or job.status != JobStatus.AWAITING_APPROVAL:
                raise ValueError("job is not awaiting approval")
            source = self.settings.processing_root / "jobs" / job_id / "input"
            if not source.is_dir():
                source = resolve_source(self.settings.incoming_root, job.source_key)
            items = list(
                session.scalars(
                    select(InventoryFile).where(InventoryFile.job_id == job_id)
                )
            )
            artifacts = list(
                session.scalars(
                    select(ProcessingArtifact).where(
                        ProcessingArtifact.job_id == job_id,
                        ProcessingArtifact.status == "validated",
                    )
                )
            )
            artifact_inputs = {a.inventory_file_id for a in artifacts}
            disc_artifacts = bool(artifacts)
            planned: list[PublicationEntry] = []
            artifact_rows: list[tuple[ProcessingArtifact, InventoryFile]] = []
            for artifact in artifacts:
                item = session.get(InventoryFile, artifact.inventory_file_id)
                if not item or not item.platform:
                    continue
                artifact_rows.append((artifact, item))
            artifact_groups: dict[
                str, list[tuple[ProcessingArtifact, InventoryFile]]
            ] = {}
            for artifact, item in artifact_rows:
                title = disc_group_name(
                    item.game_guess or Path(item.relative_path).stem
                )
                key = f"{item.platform}:{title.casefold()}"
                artifact_groups.setdefault(key, []).append((artifact, item))
            for group_key, group in artifact_groups.items():
                group.sort(key=lambda pair: pair[0].relative_path.casefold())
                multi_disc = len(group) > 1
                playlist_entries: list[PublicationEntry] = []
                for number, (artifact, item) in enumerate(group, start=1):
                    assert item.platform is not None
                    game_name = disc_group_name(
                        item.game_guess or Path(item.relative_path).stem
                    )
                    stem = f"{game_name} (Disc {number})" if multi_disc else game_name
                    target = Path(item.platform) / (
                        f"{stem}{Path(artifact.relative_path).suffix.casefold()}"
                    )
                    entry = PublicationEntry(
                        job_id=job_id,
                        inventory_file_id=item.id,
                        group_key=group_key[:128],
                        source_path=str(
                            self.settings.processing_root
                            / "jobs"
                            / job_id
                            / artifact.relative_path
                        ),
                        target_path=str(target),
                        sha256=artifact.sha256 or "",
                        status="planned",
                    )
                    planned.append(entry)
                    playlist_entries.append(entry)
                if multi_disc and all(
                    Path(entry.target_path).suffix.casefold() == ".chd"
                    for entry in playlist_entries
                ):
                    playlist = (
                        self.settings.processing_root
                        / "jobs"
                        / job_id
                        / "playlists"
                        / f"{group_key[:96]}.m3u"
                    )
                    playlist.parent.mkdir(parents=True, exist_ok=True)
                    playlist.write_text(
                        "#EXTM3U\n"
                        + "\n".join(
                            Path(entry.target_path).name for entry in playlist_entries
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    planned.append(
                        PublicationEntry(
                            job_id=job_id,
                            inventory_file_id=None,
                            group_key=group_key[:128],
                            source_path=str(playlist),
                            target_path=str(
                                Path(playlist_entries[0].target_path).parent
                                / f"{disc_group_name(group[0][1].game_guess or Path(group[0][1].relative_path).stem)}.m3u"
                            ),
                            sha256=sha256_file(playlist),
                            status="planned",
                        )
                    )
            for item in items:
                if item.dat_status != "matched" or item.processing_status not in {
                    "ready",
                    "validated",
                }:
                    continue
                if item.id in artifact_inputs or (
                    disc_artifacts and item.extension in {".cue", ".bin"}
                ):
                    continue
                input_path = (source / item.relative_path).resolve(strict=True)
                if not input_path.is_relative_to(source) or not item.sha256:
                    continue
                if item.is_bios:
                    target = Path(input_path.name)
                elif item.platform:
                    target = Path(item.platform) / (
                        f"{safe_name(item.game_guess or input_path.stem)}"
                        f"{input_path.suffix.casefold()}"
                    )
                else:
                    continue
                planned.append(
                    PublicationEntry(
                        job_id=job_id,
                        inventory_file_id=item.id,
                        group_key=f"source-{item.id}",
                        source_path=str(input_path),
                        target_path=str(target),
                        sha256=item.sha256,
                        is_bios=item.is_bios,
                        status="planned",
                    )
                )
            session.add_all(planned)
            self._log(
                session, job_id, "info", f"Prepared {len(planned)} publication entries"
            )
            return planned

    def run(self, job_id: str, scan_only: bool = False) -> None:
        try:
            if scan_only:
                self._scan(job_id)
                return
            entries = self.plan(job_id)
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if not job:
                    return
                job.status = JobStatus.PUBLISHING
                self._log(session, job_id, "info", "Publishing started")
            group_keys = dict.fromkeys(entry.group_key for entry in entries)
            for group_key in group_keys:
                self._publish_group(job_id, group_key)
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if not job:
                    return
                job.publication_completed_at = datetime.now(UTC)
                job.status = JobStatus.SCANNING
                self._log(session, job_id, "info", "Publishing completed")
            self._scan(job_id)
        except Exception as exc:
            self._rollback(job_id)
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_detail = str(exc)
                    job.finished_at = datetime.now(UTC)
                    self._log(session, job_id, "error", f"Publishing failed: {exc}")

    def _rollback(self, job_id: str) -> None:
        """Remove only files this job created and can still prove by checksum."""
        with self.factory() as session:
            job = session.get(Job, job_id)
            if not job or job.publication_completed_at:
                return
            entries = list(
                session.scalars(
                    select(PublicationEntry).where(
                        PublicationEntry.job_id == job_id,
                        PublicationEntry.status == "published",
                    )
                )
            )
        for entry in entries:
            root = (
                self.settings.library_bios_root
                if entry.is_bios
                else self.settings.library_roms_root
            )
            target = (root / entry.target_path).resolve()
            if (
                target.is_relative_to(root.resolve())
                and target.is_file()
                and sha256_file(target) == entry.sha256
            ):
                target.unlink()
                with self.factory.begin() as session:
                    current = session.get(PublicationEntry, entry.id)
                    if current:
                        current.status = "rolled_back"
                        current.detail = "removed after publication failure"

    def _publish_entry(self, entry_id: int) -> None:
        with self.factory() as session:
            entry = session.get(PublicationEntry, entry_id)
            if not entry:
                return
            source = Path(entry.source_path).resolve(strict=True)
            root = (
                self.settings.library_bios_root
                if entry.is_bios
                else self.settings.library_roms_root
            )
            target = (root / entry.target_path).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError("publication target escaped library root")
            if entry.status in {"published", "skipped"}:
                return
        if not source.is_file() or sha256_file(source) != entry.sha256:
            raise ValueError(f"source verification failed: {entry.source_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            status = "skipped" if sha256_file(target) == entry.sha256 else "conflict"
            with self.factory.begin() as session:
                current = session.get(PublicationEntry, entry_id)
                if current:
                    current.status = status
                    current.detail = (
                        "identical target exists"
                        if status == "skipped"
                        else "target collision"
                    )
            return
        stage = root / ".romm-importer-staging" / entry.job_id / entry.target_path
        stage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, stage)
        if sha256_file(stage) != entry.sha256:
            stage.unlink(missing_ok=True)
            raise ValueError(f"staging verification failed: {entry.target_path}")
        os.replace(stage, target)
        with self.factory.begin() as session:
            current = session.get(PublicationEntry, entry_id)
            if current:
                current.status = "published"
                current.published_at = datetime.now(UTC)

    def _publish_group(self, job_id: str, group_key: str) -> None:
        """Refuse a multi-file game as a unit when any target would collide."""
        with self.factory() as session:
            entries = list(
                session.scalars(
                    select(PublicationEntry).where(
                        PublicationEntry.job_id == job_id,
                        PublicationEntry.group_key == group_key,
                    )
                )
            )
            conflict_ids: list[int] = []
            for entry in entries:
                root = (
                    self.settings.library_bios_root
                    if entry.is_bios
                    else self.settings.library_roms_root
                )
                target = (root / entry.target_path).resolve()
                if (
                    target.is_relative_to(root.resolve())
                    and target.exists()
                    and (not target.is_file() or sha256_file(target) != entry.sha256)
                ):
                    conflict_ids.append(entry.id)
        if conflict_ids:
            with self.factory.begin() as session:
                for entry in session.scalars(
                    select(PublicationEntry).where(
                        PublicationEntry.job_id == job_id,
                        PublicationEntry.group_key == group_key,
                    )
                ):
                    entry.status = "conflict" if entry.id in conflict_ids else "blocked"
                    entry.detail = "publication group contains a target collision"
            return
        for entry in entries:
            self._publish_entry(entry.id)

    def _scan(self, job_id: str) -> None:
        with self.factory.begin() as session:
            job = session.get(Job, job_id)
            if not job or not job.publication_completed_at:
                raise ValueError("publication has not completed")
            job.status = JobStatus.SCANNING
            job.scan_attempts += 1
            slugs = [
                part.split("/", 1)[0]
                for part in session.scalars(
                    select(PublicationEntry.target_path).where(
                        PublicationEntry.job_id == job_id,
                        PublicationEntry.is_bios.is_(False),
                        PublicationEntry.status.in_(["published", "skipped"]),
                    )
                )
            ]
        try:
            client = RommClient(self.settings)
            task_id = client.start_scan(slugs)
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job:
                    job.scan_task_id = task_id
                    job.scan_status = "queued"
            status = client.wait_for_scan(
                task_id, self.settings.romm_scan_timeout_seconds
            )
        except RommError as exc:
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job:
                    job.scan_status = "failed"
                    job.scan_error = str(exc)
            raise
        with self.factory.begin() as session:
            job = session.get(Job, job_id)
            if job:
                job.status = JobStatus.COMPLETED
                job.scan_status = status
                job.scan_error = None
                job.finished_at = datetime.now(UTC)
                self._log(session, job_id, "info", "RomM scan completed")
        self._cleanup_work(job_id)

    def _cleanup_work(self, job_id: str) -> None:
        """Remove only this completed job's disposable processing artefacts."""
        work_root = self.settings.processing_root.resolve()
        work = (work_root / "jobs" / job_id).resolve()
        if not work.is_relative_to(work_root) or not work.exists():
            return
        try:
            shutil.rmtree(work)
        except OSError as exc:
            with self.factory.begin() as session:
                self._log(session, job_id, "warning", f"Work cleanup failed: {exc}")
        else:
            with self.factory.begin() as session:
                self._log(session, job_id, "info", "Processing artefacts cleaned up")
