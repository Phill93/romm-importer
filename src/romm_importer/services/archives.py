"""Bounded archive preparation; originals are never modified."""

import shutil
import subprocess
import zipfile
from pathlib import Path

from ..config import Settings

ARCHIVES = {".zip", ".7z"}


def _safe_member(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not name:
        raise ValueError("archive member escapes extraction root")
    return path


def _check_tree(root: Path, settings: Settings) -> None:
    count = total = 0
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError("archive contains symbolic link")
        if item.is_file():
            count += 1
            size = item.stat().st_size
            total += size
            if (
                count > settings.max_archive_entries
                or size > settings.max_archive_file_bytes
                or total > settings.max_archive_total_bytes
            ):
                raise ValueError("archive extraction exceeds configured limits")


def validate_7z_listing(output: str, settings: Settings) -> None:
    """Validate 7z's technical listing before extraction touches disk."""
    if "----------" not in output:
        raise ValueError("7z did not produce a valid archive listing")
    count = total = 0
    for record in output.split("----------", 1)[1].split("\n\n"):
        fields = {
            key.strip(): value.strip()
            for line in record.splitlines()
            if " = " in line
            for key, value in [line.split(" = ", 1)]
        }
        name = fields.get("Path")
        if not name:
            continue
        _safe_member(name)
        if fields.get("Attributes", "").lstrip().startswith("l"):
            raise ValueError("archive contains symbolic link")
        size = int(fields.get("Size", "0"))
        count += 1
        total += size
        if (
            count > settings.max_archive_entries
            or size > settings.max_archive_file_bytes
            or total > settings.max_archive_total_bytes
        ):
            raise ValueError("archive exceeds configured limits")


def prepare_source(source: Path, destination: Path, settings: Settings) -> Path:
    archives = [
        p for p in source.rglob("*") if p.is_file() and p.suffix.casefold() in ARCHIVES
    ]
    if not archives:
        return source
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix.casefold() in ARCHIVES
        ):
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for archive in archives:
        target = destination / archive.relative_to(source).with_suffix("")
        target.mkdir(parents=True, exist_ok=True)
        if archive.suffix.casefold() == ".zip":
            with zipfile.ZipFile(archive) as handle:
                members = handle.infolist()
                if len(members) > settings.max_archive_entries:
                    raise ValueError("archive has too many members")
                total = 0
                for member in members:
                    _safe_member(member.filename)
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("archive contains symbolic link")
                    total += member.file_size
                    if (
                        member.file_size > settings.max_archive_file_bytes
                        or total > settings.max_archive_total_bytes
                    ):
                        raise ValueError("archive exceeds configured limits")
                handle.extractall(target, members)
        else:
            listing = subprocess.run(
                ["7z", "l", "-slt", str(archive)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            validate_7z_listing(listing.stdout, settings)
            subprocess.run(
                ["7z", "x", "-y", f"-o{target}", str(archive)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        _check_tree(target, settings)
    return destination
