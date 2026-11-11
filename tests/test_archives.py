import zipfile
from pathlib import Path

import pytest

from romm_importer.services.archives import prepare_source, validate_7z_listing


def test_zip_is_extracted_into_working_copy(settings, tmp_path: Path) -> None:
    source = settings.incoming_root / "set"
    source.mkdir()
    with zipfile.ZipFile(source / "games.zip", "w") as archive:
        archive.writestr("nested/game.gb", b"rom")
    prepared = prepare_source(source, tmp_path / "work", settings)
    assert prepared != source
    assert (prepared / "games" / "nested" / "game.gb").read_bytes() == b"rom"
    assert (source / "games.zip").is_file()


def test_zip_slip_is_rejected(settings, tmp_path: Path) -> None:
    source = settings.incoming_root / "set"
    source.mkdir()
    with zipfile.ZipFile(source / "bad.zip", "w") as archive:
        archive.writestr("../outside.gb", b"bad")
    with pytest.raises(ValueError, match="escapes"):
        prepare_source(source, tmp_path / "work", settings)


def test_non_archive_source_is_not_copied(settings, tmp_path: Path) -> None:
    source = settings.incoming_root / "set"
    source.mkdir()
    (source / "game.gb").write_bytes(b"rom")
    assert prepare_source(source, tmp_path / "work", settings) == source


def test_7z_listing_is_validated_before_extraction(settings) -> None:
    listing = """Path = games.7z
Type = 7z
----------
Path = nested/game.gb
Size = 3
Attributes = A

Path = nested/readme.txt
Size = 4
Attributes = A
"""
    validate_7z_listing(listing, settings)
    unsafe = listing.replace("nested/game.gb", "../outside.gb")
    with pytest.raises(ValueError, match="escapes"):
        validate_7z_listing(unsafe, settings)
