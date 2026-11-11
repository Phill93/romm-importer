import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from romm_importer.db import Base, build_engine, make_session_factory
from romm_importer.models import DatSnapshot
from romm_importer.services.dats import _safe_filename, snapshot_path, store_snapshot


def test_dat_snapshot_is_immutable_and_deduplicated(settings, tmp_path) -> None:
    settings.dats_root = tmp_path / "dats"
    engine = build_engine(settings.database_path)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        first = asyncio.run(
            store_snapshot(
                session,
                settings,
                UploadFile(BytesIO(b"<datafile/>"), filename="No-Intro.dat"),
                "No-Intro",
                "https://example.invalid/dats",
            )
        )
        duplicate = asyncio.run(
            store_snapshot(
                session,
                settings,
                UploadFile(BytesIO(b"<datafile/>"), filename="same.xml"),
                "other",
                None,
            )
        )
        assert first.id == duplicate.id
        assert snapshot_path(settings, first).read_bytes() == b"<datafile/>"
    with factory() as session:
        assert len(list(session.scalars(select(DatSnapshot)))) == 1


def test_dat_filename_rejects_traversal_and_unknown_types() -> None:
    with pytest.raises(HTTPException):
        _safe_filename("../outside.dat")
    with pytest.raises(HTTPException):
        _safe_filename("not-a-dat.exe")
