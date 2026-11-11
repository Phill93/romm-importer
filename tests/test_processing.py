import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from romm_importer.db import Base, build_engine, make_session_factory
from romm_importer.models import (
    DatSnapshot,
    InventoryFile,
    Job,
    JobStatus,
    PlatformRule,
)
from romm_importer.services.processing import (
    ProcessCancelled,
    ProcessingWorker,
    ProcessRunner,
    cue_references,
    ps2_image_kind,
)


def _factory(settings):
    engine = build_engine(settings.database_path)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_cue_references_and_ps2_signatures(settings) -> None:
    cue = settings.incoming_root / "disc.cue"
    track = settings.incoming_root / "track 01.bin"
    track.write_bytes(b"track")
    cue.write_text('FILE "track 01.bin" BINARY\n', encoding="utf-8")
    assert cue_references(cue) == [track.resolve()]
    cue.write_text('FILE "../outside.bin" BINARY\n', encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        cue_references(cue)

    cd = settings.incoming_root / "disc.iso"
    cd.write_bytes(b"CD001" + b"\0" * 32)
    assert ps2_image_kind(cd) == "cd"
    dvd = settings.incoming_root / "dvd.iso"
    dvd.write_bytes(b"header NSR02")
    assert ps2_image_kind(dvd) == "dvd"


def test_process_runner_cancels_its_process_group(settings, tmp_path: Path) -> None:
    settings.process_timeout_seconds = 10
    runner = ProcessRunner(settings)
    with pytest.raises(ProcessCancelled, match="cancelled"):
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            cancel_requested=lambda: True,
        )


class _ToolRunner:
    def __init__(self, fail_verify: bool = False) -> None:
        self.fail_verify = fail_verify
        self.commands: list[list[str]] = []

    def run(
        self,
        args: list[str],
        _cwd: Path,
        _extra_env: dict[str, str] | None = None,
        cancel_requested=None,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        assert cancel_requested is None or not cancel_requested()
        if args[0] == "igir":
            report = Path(args[args.index("--report-output") + 1])
            report.write_text(
                "Input,Status,Game Name,DAT,BIOS\n"
                "disc.cue,FOUND,Example (Disc 1),Test DAT,false\n",
                encoding="utf-8",
            )
        elif args[1] in {"createcd", "createdvd"}:
            Path(args[args.index("-o") + 1]).write_bytes(b"chd")
        elif args[1] == "verify" and self.fail_verify:
            raise RuntimeError("verification failed")
        return subprocess.CompletedProcess(args, 0, "tool output", "")


def _processing_job(settings, factory, *, platform: str = "psx") -> int:
    dat = settings.dats_root / "test.dat"
    dat.parent.mkdir(parents=True)
    dat.write_text("dat", encoding="utf-8")
    cue = settings.incoming_root / "disc.cue"
    cue.write_text('FILE "track.bin" BINARY\n', encoding="utf-8")
    (settings.incoming_root / "track.bin").write_bytes(b"track")
    with factory.begin() as session:
        snapshot = DatSnapshot(
            filename="test.dat",
            stored_path=str(dat),
            sha256="a" * 64,
            source_label="test",
            size_bytes=3,
        )
        session.add(snapshot)
        session.flush()
        session.add(
            Job(
                id="process",
                name="process",
                source_key=".",
                dat_snapshot_id=snapshot.id,
                status=JobStatus.ANALYZING,
            )
        )
        session.add(
            PlatformRule(
                source_name="psx-source",
                romm_directory=platform,
                extensions=".cue,.bin",
                target_format="chd",
                converter="chd_cd",
            )
        )
        item = InventoryFile(
            job_id="process",
            relative_path="disc.cue",
            size_bytes=cue.stat().st_size,
            extension=".cue",
            platform=platform,
            dat_status="pending",
        )
        session.add(item)
        session.flush()
        return item.id


def test_processing_runs_mocked_igir_chd_and_verification(settings) -> None:
    factory = _factory(settings)
    item_id = _processing_job(settings, factory)
    worker = ProcessingWorker(factory, settings)
    tool = _ToolRunner()
    worker.runner = tool  # type: ignore[assignment]
    worker.run("process")
    with factory() as session:
        job = session.get(Job, "process")
        item = session.get(InventoryFile, item_id)
        assert job is not None and job.status == JobStatus.AWAITING_APPROVAL
        assert item is not None and item.processing_status == "validated"
    assert any(command[1] == "createcd" for command in tool.commands)
    assert any(command[1] == "verify" for command in tool.commands)


def test_chd_verification_failure_marks_job_failed(settings) -> None:
    factory = _factory(settings)
    _processing_job(settings, factory)
    worker = ProcessingWorker(factory, settings)
    worker.runner = _ToolRunner(fail_verify=True)  # type: ignore[assignment]
    worker.run("process")
    assert (settings.quarantine_root / "process").is_dir()
    assert not (settings.processing_root / "jobs" / "process").exists()
    with factory() as session:
        job = session.get(Job, "process")
        assert job is not None and job.status == JobStatus.FAILED
        assert job.error_detail == "verification failed"
        assert list(session.scalars(select(InventoryFile.id)))
