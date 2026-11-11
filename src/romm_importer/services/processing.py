"""Igir analysis and conservative CHD staging."""

import csv
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import (
    DatSnapshot,
    InventoryFile,
    Job,
    JobLog,
    JobStatus,
    PlatformRule,
    ProcessingArtifact,
)
from .dats import snapshot_path
from .inventory import resolve_source, sha256_file

CUE_FILE = re.compile(r'^\s*FILE\s+"([^"]+)"', re.IGNORECASE | re.MULTILINE)


class ProcessCancelled(RuntimeError):
    """Raised after terminating a job's complete tool process group."""


class ProcessRunner:
    """Runs fixed command vectors and terminates their complete process group."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        args: list[str],
        cwd: Path,
        extra_env: dict[str, str] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if extra_env:
            environment.update(extra_env)
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=environment,
        )
        deadline = time.monotonic() + self.settings.process_timeout_seconds
        while True:
            try:
                output, _ = process.communicate(
                    timeout=min(1, max(0.1, deadline - time.monotonic()))
                )
                break
            except subprocess.TimeoutExpired:
                if cancel_requested and cancel_requested():
                    self._stop_process_group(process)
                    raise ProcessCancelled(
                        f"job cancelled while running {args[0]}"
                    ) from None
                if time.monotonic() < deadline:
                    continue
                self._stop_process_group(process)
                raise TimeoutError(f"process timed out: {args[0]}") from None

        if process.returncode:
            if "Reached heap limit" in output:
                raise RuntimeError(
                    "Igir exhausted its Node.js heap while parsing the selected DAT "
                    "snapshot. Choose a smaller platform DAT or raise "
                    "IGIR_MAX_OLD_SPACE_MB (maximum 1536 with the 2 GiB limit)."
                )
            raise RuntimeError(
                f"process failed ({process.returncode}): {output[-2000:]}"
            )
        return subprocess.CompletedProcess(args, process.returncode, output, "")

    def _stop_process_group(self, process: subprocess.Popen[str]) -> None:
        """Terminate the tool and any child processes created in its session."""
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=self.settings.process_kill_grace_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


def cue_references(cue: Path) -> list[Path]:
    """Return only safe, local CUE references."""
    paths: list[Path] = []
    for raw in CUE_FILE.findall(cue.read_text(encoding="utf-8", errors="replace")):
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("CUE references a path outside its disc set")
        candidate = (cue.parent / relative).resolve(strict=True)
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or not candidate.is_relative_to(cue.parent.resolve(strict=True))
        ):
            raise ValueError(f"invalid CUE reference: {raw}")
        paths.append(candidate)
    if not paths:
        raise ValueError("CUE has no referenced track files")
    return paths


def ps2_image_kind(image: Path) -> str | None:
    """Classify only strong signatures; ambiguous images must be reviewed."""
    data = image.read_bytes()[: 1024 * 1024]
    if b"NSR02" in data or b"NSR03" in data:
        return "dvd"
    if image.stat().st_size <= 900 * 1024 * 1024 and b"CD001" in data:
        return "cd"
    return None


class ProcessingWorker:
    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory = factory
        self.settings = settings
        self.runner = ProcessRunner(settings)

    def _log(self, session: Session, job_id: str, level: str, message: str) -> None:
        session.add(JobLog(job_id=job_id, level=level, message=message))

    def _cancel_requested(self, job_id: str) -> bool:
        with self.factory() as session:
            job = session.get(Job, job_id)
            return bool(job and job.cancel_requested)

    def _quarantine_work(self, job_id: str) -> None:
        """Move only this job's disposable working tree out of processing."""
        work = self.settings.processing_root / "jobs" / job_id
        quarantine = self.settings.quarantine_root / job_id
        if work.is_dir() and not quarantine.exists():
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work), str(quarantine))

    def run(self, job_id: str) -> None:
        try:
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if not job or not job.dat_snapshot_id:
                    return
                snapshot = session.get(DatSnapshot, job.dat_snapshot_id)
                if not snapshot:
                    raise ValueError("selected DAT snapshot does not exist")
                job.status = JobStatus.ANALYZING
                self._log(session, job_id, "info", "Igir DAT analysis started")
                source = self.settings.processing_root / "jobs" / job_id / "input"
                if not source.is_dir():
                    source = resolve_source(self.settings.incoming_root, job.source_key)
                dat = snapshot_path(self.settings, snapshot)
            work = self.settings.processing_root / "jobs" / job_id
            report_dir = self.settings.reports_root / job_id
            work.mkdir(parents=True, exist_ok=True)
            report_dir.mkdir(parents=True, exist_ok=True)
            report = report_dir / "igir-report.csv"
            result = self.runner.run(
                [
                    "igir",
                    "report",
                    "--dat",
                    str(dat),
                    "--input",
                    str(source),
                    "--report-output",
                    str(report),
                    "--dat-threads",
                    "1",
                    "--reader-threads",
                    "1",
                    "--temp-dir",
                    str(work / "tmp"),
                ],
                work,
                {
                    "NODE_OPTIONS": (
                        f"--max-old-space-size={self.settings.igir_max_old_space_mb}"
                    )
                },
                cancel_requested=lambda: self._cancel_requested(job_id),
            )
            (report_dir / "igir-output.log").write_text(result.stdout, encoding="utf-8")
            self._apply_report(job_id, report)
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job:
                    job.status = JobStatus.CONVERTING
                    self._log(session, job_id, "info", "Disc conversion started")
            self._convert_discs(job_id, source, work)
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return
                job.status = JobStatus.VALIDATING
                self._log(session, job_id, "info", "Validation complete")
                job.status = JobStatus.AWAITING_APPROVAL
                job.finished_at = datetime.now(UTC)
                self._log(session, job_id, "info", "Phase 2 review ready")
        except ProcessCancelled:
            self._quarantine_work(job_id)
            with self.factory.begin() as session:
                job = session.get(Job, job_id)
                if job:
                    job.status = JobStatus.CANCELLED
                    job.finished_at = datetime.now(UTC)
                    self._log(session, job_id, "info", "Processing cancelled")
        except Exception as exc:
            self._fail(job_id, str(exc))

    def _apply_report(self, job_id: str, report: Path) -> None:
        if not report.is_file():
            raise ValueError("Igir did not create its report")
        rows = list(csv.DictReader(report.open(encoding="utf-8-sig", newline="")))
        with self.factory.begin() as session:
            files = list(
                session.scalars(
                    select(InventoryFile).where(InventoryFile.job_id == job_id)
                )
            )
            by_name = {item.relative_path.casefold(): item for item in files}
            for row in rows:
                input_path = (
                    row.get("Input")
                    or row.get("Input file")
                    or row.get("Input path")
                    or row.get("ROM Files")
                    or ""
                )
                status = (row.get("Status") or "UNUSED").casefold()
                candidates = [
                    item
                    for path, item in by_name.items()
                    if path in input_path.casefold()
                    or Path(path).name.casefold() in input_path.casefold()
                ]
                for item in candidates:
                    item.dat_status = "matched" if status == "found" else status
                    item.dat_name = row.get("DAT") or row.get("DAT Name")
                    item.dat_version = row.get("DAT version")
                    item.is_bios = str(row.get("BIOS", "false")).casefold() == "true"
                    if row.get("Game Name"):
                        item.game_guess = str(row["Game Name"])
                    item.processing_status = (
                        "ready" if item.dat_status == "matched" else "review"
                    )

    def _convert_discs(self, job_id: str, source: Path, work: Path) -> None:
        with self.factory() as session:
            converters = {
                rule.romm_directory: rule.converter
                for rule in session.scalars(select(PlatformRule))
            }
            files = list(
                session.scalars(
                    select(InventoryFile).where(
                        InventoryFile.job_id == job_id,
                        InventoryFile.dat_status == "matched",
                    )
                )
            )
        for item in files:
            converter = converters.get(item.platform or "")
            if converter not in {"chd_cd", "chd_auto"}:
                continue
            path = (source / item.relative_path).resolve(strict=True)
            if not path.is_relative_to(source):
                continue
            if path.suffix.casefold() == ".cue":
                self._convert_cue(job_id, item, path, work)
            elif converter == "chd_auto" and path.suffix.casefold() == ".iso":
                kind = ps2_image_kind(path)
                if kind:
                    self._convert_iso(job_id, item, path, work, kind)
                else:
                    self._mark_review(job_id, item.id, "ambiguous PS2 disc type")

    def _stage_cue(self, cue: Path, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        for item in [cue, *cue_references(cue)]:
            shutil.copy2(item, destination / item.name)
        return destination / cue.name

    def _convert_cue(
        self, job_id: str, item: InventoryFile, cue: Path, work: Path
    ) -> None:
        staging = self._stage_cue(cue, work / "discs" / str(item.id))
        output = work / "converted" / f"{cue.stem}.chd"
        output.parent.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                "chdman",
                "createcd",
                "-i",
                str(staging),
                "-o",
                str(output),
                "-np",
                "2",
            ],
            work,
            cancel_requested=lambda: self._cancel_requested(job_id),
        )
        self._verify(job_id, item, output, work)

    def _convert_iso(
        self, job_id: str, item: InventoryFile, image: Path, work: Path, kind: str
    ) -> None:
        output = work / "converted" / f"{image.stem}.chd"
        output.parent.mkdir(parents=True, exist_ok=True)
        command = "createdvd" if kind == "dvd" else "createcd"
        self.runner.run(
            ["chdman", command, "-i", str(image), "-o", str(output), "-np", "2"],
            work,
            cancel_requested=lambda: self._cancel_requested(job_id),
        )
        self._verify(job_id, item, output, work)

    def _verify(
        self, job_id: str, item: InventoryFile, output: Path, work: Path
    ) -> None:
        self.runner.run(
            ["chdman", "verify", "-i", str(output)],
            work,
            cancel_requested=lambda: self._cancel_requested(job_id),
        )
        with self.factory.begin() as session:
            current = session.get(InventoryFile, item.id)
            if current:
                current.processing_status = "validated"
                current.proposed_target = str(output.relative_to(work))
            session.add(
                ProcessingArtifact(
                    job_id=job_id,
                    inventory_file_id=item.id,
                    kind="chd",
                    relative_path=str(output.relative_to(work)),
                    sha256=sha256_file(output),
                    status="validated",
                )
            )

    def _mark_review(self, job_id: str, item_id: int, detail: str) -> None:
        with self.factory.begin() as session:
            item = session.get(InventoryFile, item_id)
            if item:
                item.processing_status = "review"
                item.error_detail = detail
                self._log(session, job_id, "warning", detail)

    def _fail(self, job_id: str, detail: str) -> None:
        self._quarantine_work(job_id)
        with self.factory.begin() as session:
            job = session.get(Job, job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error_detail = detail
                job.finished_at = datetime.now(UTC)
                self._log(session, job_id, "error", "Phase 2 processing failed")
