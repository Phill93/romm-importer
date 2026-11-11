"""Immutable DAT snapshot storage."""

import hashlib
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import DatSnapshot

ALLOWED_DAT_SUFFIXES = {".dat", ".xml", ".zip", ".7z"}


def _safe_filename(name: str) -> str:
    candidate = Path(name)
    if (
        not candidate.name
        or candidate.name != name
        or candidate.suffix.casefold() not in ALLOWED_DAT_SUFFIXES
    ):
        raise HTTPException(422, "Only DAT, XML, ZIP, and 7Z DAT snapshots are allowed")
    return candidate.name


async def store_snapshot(
    session: Session,
    settings: Settings,
    upload: UploadFile,
    source_label: str,
    source_url: str | None,
) -> DatSnapshot:
    filename = _safe_filename(upload.filename or "")
    settings.dats_root.mkdir(parents=True, exist_ok=True)
    temporary = settings.dats_root / f".upload-{filename}"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_dat_upload_bytes:
                    raise HTTPException(413, "DAT upload exceeds configured limit")
                digest.update(chunk)
                output.write(chunk)
        checksum = digest.hexdigest()
        existing = session.scalar(
            select(DatSnapshot).where(DatSnapshot.sha256 == checksum)
        )
        if existing:
            return existing
        stored = settings.dats_root / f"{checksum}-{filename}"
        temporary.replace(stored)
        snapshot = DatSnapshot(
            filename=filename,
            stored_path=str(stored),
            sha256=checksum,
            source_label=source_label.strip() or "manual upload",
            source_url=source_url.strip() if source_url else None,
            size_bytes=size,
        )
        session.add(snapshot)
        session.flush()
        return snapshot
    finally:
        temporary.unlink(missing_ok=True)
        await upload.close()


def snapshot_path(settings: Settings, snapshot: DatSnapshot) -> Path:
    path = Path(snapshot.stored_path).resolve(strict=True)
    if not path.is_relative_to(settings.dats_root.resolve(strict=True)):
        raise ValueError("DAT snapshot escaped DAT storage")
    return path
