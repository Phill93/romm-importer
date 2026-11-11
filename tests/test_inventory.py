import pytest
from sqlalchemy import select

from romm_importer.db import Base, build_engine, make_session_factory
from romm_importer.models import InventoryFile, Job, JobStatus
from romm_importer.services.inventory import InventoryWorker, resolve_source, seed_rules


def test_source_rejects_traversal(settings) -> None:
    with pytest.raises(ValueError):
        resolve_source(settings.incoming_root, "../outside")


def test_inventory_hashes_and_maps_gc_to_ngc(settings) -> None:
    source = settings.incoming_root / "gc"
    source.mkdir()
    (source / "Example.iso").write_bytes(b"rom-data")
    engine = build_engine(settings.database_path)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        seed_rules(session)
        session.add(Job(id="job-1", name="test", source_key="."))
    InventoryWorker(factory, settings).run("job-1")
    with factory() as session:
        job = session.get(Job, "job-1")
        file = session.scalar(
            select(InventoryFile).where(InventoryFile.job_id == "job-1")
        )
        assert job is not None and job.status == JobStatus.AWAITING_APPROVAL
        assert file is not None and file.platform == "ngc"
        assert file.sha256 is not None and len(file.sha256) == 64


def test_inventory_excludes_symlink(settings) -> None:
    target = settings.incoming_root / "real.gb"
    target.write_bytes(b"game")
    (settings.incoming_root / "link.gb").symlink_to(target)
    engine = build_engine(settings.database_path)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        seed_rules(session)
        session.add(Job(id="job-2", name="test", source_key="."))
    InventoryWorker(factory, settings).run("job-2")
    with factory() as session:
        files = list(
            session.scalars(
                select(InventoryFile).where(InventoryFile.job_id == "job-2")
            )
        )
    assert [item.relative_path for item in files] == ["real.gb"]
