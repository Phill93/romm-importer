"""Persistent entities."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class JobStatus(StrEnum):
    CREATED = "created"
    INVENTORYING = "inventorying"
    ANALYZING = "analyzing"
    CONVERTING = "converting"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    PUBLISHING = "publishing"
    SCANNING = "scanning"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    source_key: Mapped[str] = mapped_column(String(512))
    dat_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("dat_snapshots.id"), nullable=True
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.CREATED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    successful_files: Mapped[int] = mapped_column(Integer, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_platforms: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tool_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    publication_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    scan_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scan_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scan_attempts: Mapped[int] = mapped_column(Integer, default=0)
    scan_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    files: Mapped[list["InventoryFile"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    dat_snapshot: Mapped["DatSnapshot | None"] = relationship()


class InventoryFile(Base):
    __tablename__ = "inventory_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    relative_path: Mapped[str] = mapped_column(String(2048))
    size_bytes: Mapped[int] = mapped_column(Integer)
    extension: Mapped[str] = mapped_column(String(32))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    game_guess: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dat_status: Mapped[str] = mapped_column(String(32), default="pending")
    dat_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dat_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_bios: Mapped[bool] = mapped_column(default=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="pending")
    proposed_target: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    proposed_action: Mapped[str] = mapped_column(String(64), default="review")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    job: Mapped[Job] = relationship(back_populates="files")


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    level: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    job: Mapped[Job] = relationship(back_populates="logs")


class PlatformRule(Base):
    __tablename__ = "platform_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(64), unique=True)
    romm_directory: Mapped[str] = mapped_column(String(64))
    extensions: Mapped[str] = mapped_column(Text)
    target_format: Mapped[str] = mapped_column(String(32))
    converter: Mapped[str] = mapped_column(String(32), default="none")


class DatSnapshot(Base):
    __tablename__ = "dat_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(2048), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_label: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    size_bytes: Mapped[int] = mapped_column(Integer)


class ProcessingArtifact(Base):
    __tablename__ = "processing_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    inventory_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_files.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    relative_path: Mapped[str] = mapped_column(String(2048))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class PublicationEntry(Base):
    """Durable manifest of one safe-to-publish file."""

    __tablename__ = "publication_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    inventory_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_files.id"), nullable=True
    )
    group_key: Mapped[str] = mapped_column(String(128), index=True)
    source_path: Mapped[str] = mapped_column(String(2048))
    target_path: Mapped[str] = mapped_column(String(2048))
    sha256: Mapped[str] = mapped_column(String(64))
    is_bios: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), default="planned")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
