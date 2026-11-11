"""Small administrative CLI."""

import getpass
import sys

from argon2 import PasswordHasher


def main() -> None:
    if sys.argv[1:] != ["hash-password"]:
        print("Usage: romm-importer hash-password", file=sys.stderr)
        raise SystemExit(2)
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        print("Passwords do not match", file=sys.stderr)
        raise SystemExit(1)
    print(PasswordHasher().hash(first))
