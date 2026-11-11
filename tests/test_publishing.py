from pathlib import Path

from sqlalchemy import select

from romm_importer.db import Base, build_engine, make_session_factory
from romm_importer.models import (
    InventoryFile,
    Job,
    JobStatus,
    ProcessingArtifact,
    PublicationEntry,
)
from romm_importer.services.inventory import sha256_file
from romm_importer.services.publishing import PublishingWorker, disc_group_name


def _factory(settings):
    engine = build_engine(settings.database_path)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_multidisc_plan_creates_distinct_chds_and_playlist(settings) -> None:
    factory = _factory(settings)
    work = settings.processing_root / "jobs" / "multi" / "converted"
    work.mkdir(parents=True)
    first = work / "Example Disc 1.chd"
    second = work / "Example Disc 2.chd"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    with factory.begin() as session:
        session.add(
            Job(
                id="multi",
                name="multi",
                source_key=".",
                status=JobStatus.AWAITING_APPROVAL,
            )
        )
        session.add_all(
            [
                InventoryFile(
                    job_id="multi",
                    relative_path="Example Disc 1.cue",
                    size_bytes=1,
                    extension=".cue",
                    platform="psx",
                    game_guess="Example (Disc 1)",
                    dat_status="matched",
                    processing_status="validated",
                ),
                InventoryFile(
                    job_id="multi",
                    relative_path="Example Disc 2.cue",
                    size_bytes=1,
                    extension=".cue",
                    platform="psx",
                    game_guess="Example (Disc 2)",
                    dat_status="matched",
                    processing_status="validated",
                ),
            ]
        )
        session.flush()
        items = list(session.scalars(select(InventoryFile).order_by(InventoryFile.id)))
        session.add_all(
            [
                ProcessingArtifact(
                    job_id="multi",
                    inventory_file_id=items[0].id,
                    kind="chd",
                    relative_path="converted/Example Disc 1.chd",
                    sha256=sha256_file(first),
                    status="validated",
                ),
                ProcessingArtifact(
                    job_id="multi",
                    inventory_file_id=items[1].id,
                    kind="chd",
                    relative_path="converted/Example Disc 2.chd",
                    sha256=sha256_file(second),
                    status="validated",
                ),
            ]
        )
    entries = PublishingWorker(factory, settings).plan("multi")
    assert [entry.target_path for entry in entries] == [
        "psx/Example (Disc 1).chd",
        "psx/Example (Disc 2).chd",
        "psx/Example.m3u",
    ]
    playlist = Path(entries[-1].source_path)
    assert (
        playlist.read_text(encoding="utf-8")
        == "#EXTM3U\nExample (Disc 1).chd\nExample (Disc 2).chd\n"
    )


def test_publication_group_collision_blocks_all_members(settings) -> None:
    factory = _factory(settings)
    source = settings.processing_root / "source.chd"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new")
    target = settings.library_roms_root / "psx" / "Example.chd"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    with factory.begin() as session:
        session.add(
            Job(
                id="conflict",
                name="conflict",
                source_key=".",
                status=JobStatus.PUBLISHING,
            )
        )
        session.add_all(
            [
                PublicationEntry(
                    job_id="conflict",
                    group_key="psx:example",
                    source_path=str(source),
                    target_path="psx/Example.chd",
                    sha256=sha256_file(source),
                    status="planned",
                ),
                PublicationEntry(
                    job_id="conflict",
                    group_key="psx:example",
                    source_path=str(source),
                    target_path="psx/Example.m3u",
                    sha256=sha256_file(source),
                    status="planned",
                ),
            ]
        )
    PublishingWorker(factory, settings)._publish_group("conflict", "psx:example")
    with factory() as session:
        statuses = list(
            session.scalars(
                select(PublicationEntry.status).order_by(PublicationEntry.id)
            )
        )
    assert statuses == ["conflict", "blocked"]
    assert not (settings.library_roms_root / "psx" / "Example.m3u").exists()


def test_identical_existing_target_is_skipped_idempotently(settings) -> None:
    factory = _factory(settings)
    source = settings.processing_root / "same.gb"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"same")
    target = settings.library_roms_root / "gb" / "Same.gb"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same")
    with factory.begin() as session:
        session.add(
            Job(
                id="duplicate",
                name="duplicate",
                source_key=".",
                status=JobStatus.PUBLISHING,
            )
        )
        session.add(
            PublicationEntry(
                job_id="duplicate",
                group_key="same",
                source_path=str(source),
                target_path="gb/Same.gb",
                sha256=sha256_file(source),
                status="planned",
            )
        )
    PublishingWorker(factory, settings)._publish_group("duplicate", "same")
    with factory() as session:
        status = session.scalar(select(PublicationEntry.status))
    assert status == "skipped"
    assert target.read_bytes() == b"same"


def test_disc_group_name_removes_disc_marker() -> None:
    assert disc_group_name("Great Game (Disc 2)") == "Great Game"


def test_run_publishes_then_completes_scan(settings, monkeypatch) -> None:
    class FakeRomm:
        def __init__(self, _settings) -> None:
            pass

        def start_scan(self, slugs: list[str]) -> str:
            assert slugs == ["gb"]
            return "task-1"

        def wait_for_scan(self, task_id: str, timeout: int) -> str:
            assert task_id == "task-1"
            assert timeout == settings.romm_scan_timeout_seconds
            return "completed"

    monkeypatch.setattr("romm_importer.services.publishing.RommClient", FakeRomm)
    factory = _factory(settings)
    disposable = settings.processing_root / "jobs" / "publish" / "converted"
    disposable.mkdir(parents=True)
    (disposable / "example.chd").write_bytes(b"temporary")
    game = settings.incoming_root / "game.gb"
    game.write_bytes(b"game")
    with factory.begin() as session:
        session.add(
            Job(
                id="publish",
                name="publish",
                source_key=".",
                status=JobStatus.AWAITING_APPROVAL,
                scan_error="previous scan error",
            )
        )
        session.add(
            InventoryFile(
                job_id="publish",
                relative_path="game.gb",
                size_bytes=game.stat().st_size,
                extension=".gb",
                sha256=sha256_file(game),
                platform="gb",
                game_guess="Example",
                dat_status="matched",
                processing_status="ready",
            )
        )
    PublishingWorker(factory, settings).run("publish")
    assert (settings.library_roms_root / "gb" / "Example.gb").read_bytes() == b"game"
    assert not disposable.parent.exists()
    with factory() as session:
        job = session.get(Job, "publish")
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.scan_task_id == "task-1"
        assert job.scan_error is None


def test_failed_publication_rolls_back_files_created_by_the_job(settings) -> None:
    factory = _factory(settings)
    source = settings.processing_root / "good.gb"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"good")
    with factory.begin() as session:
        session.add(
            Job(
                id="rollback",
                name="rollback",
                source_key=".",
                status=JobStatus.AWAITING_APPROVAL,
            )
        )
        session.add_all(
            [
                PublicationEntry(
                    job_id="rollback",
                    group_key="good",
                    source_path=str(source),
                    target_path="gb/Good.gb",
                    sha256=sha256_file(source),
                    status="planned",
                ),
                PublicationEntry(
                    job_id="rollback",
                    group_key="missing",
                    source_path=str(source.with_name("missing.gb")),
                    target_path="gb/Missing.gb",
                    sha256="0" * 64,
                    status="planned",
                ),
            ]
        )
    PublishingWorker(factory, settings).run("rollback")
    assert not (settings.library_roms_root / "gb" / "Good.gb").exists()
    with factory() as session:
        job = session.get(Job, "rollback")
        entries = list(session.scalars(select(PublicationEntry.status)))
        assert job is not None and job.status == JobStatus.FAILED
    assert "rolled_back" in entries
