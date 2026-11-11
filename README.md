# RomM Importer

Self-hosted, review-first ROM import service for a RomM installation. It inventories a read-only SMB input, calculates SHA-256 checksums, analyzes files with Igir and immutable DAT snapshots, converts supported PS1/PS2 discs to verified CHD, and only publishes explicitly approved, verified results. Incoming files are never modified.

## Install

Copy .env.example to .env, set the host dataset paths and generate SESSION_SECRET with openssl rand -hex 32. Generate an Argon2 hash using docker compose run --rm romm-importer romm-importer hash-password, then set ADMIN_PASSWORD_HASH. Start with docker compose up -d --build and browse to http://athene:30062.

The container UID/GID is 10001. Give it traverse/read ACLs only on incoming and explicit write ACLs only on appdata, processing, quarantine and reports. Never apply recursive ACL changes to the complete dataset.

## Operations

The SQLite database is in APPDATA_PATH. Back it up while the service is stopped or with SQLite online backup. To migrate from /mnt/data/romm to /mnt/external/romm, stop the service, move appdata with a ZFS-aware operation, change only the path variables in .env, then restart. Jobs active on restart become interrupted and never resume automatically.

## Import and publication workflow

Jobs start with an inventory and remain reviewable in the browser. Import review is grouped by detected game and is still server-side paginated. DAT snapshots are manually uploaded and retained with their source label, URL and SHA-256.

ZIP and 7Z inputs are expanded only into the per-job processing area; originals remain untouched. ZIP member paths, symbolic links, member count, member size and total extraction size are restricted. 7Z extraction uses a fixed command vector and the resulting tree is checked again for symbolic links and limits.

PS1 CUE/BIN and suitable PS2 CUE/ISO files are staged as CHD and verified with `chdman verify`. Ambiguous or invalid disc sets remain in review. Multi-disc CHD games are published as one logical group with stable `Disc N` names and a generated M3U referencing those final names. If any target in a group conflicts, the entire group is withheld. Cancelling a running Igir/CHDman command terminates its process group; failed or cancelled processing artefacts are moved to that job's quarantine directory. After a successful RomM scan, disposable processing artefacts are removed while reports remain retained.

Jobs in `awaiting_approval` can be explicitly released from the job page. The importer only publishes DAT-matched, verified ROMs and CHD artifacts; existing different files are reported as conflicts and never overwritten. Files are first copied and checksummed below `.romm-importer-staging/` on the destination dataset before they become visible. If publication itself fails before completion, only files proven to have been created by that job are rolled back. A completed publication is never rolled back merely because its subsequent RomM scan fails.

Set `ROMM_API_TOKEN` to a RomM token with the minimal `tasks.run` scope. The importer uses the installed RomM 5.4 task API (`POST /api/tasks/scan`) for a quick, platform-limited scan. A failed scan never republishes files; use **RomM-Scan erneut versuchen** on the job page.

The container runs as UID/GID `10001`. Grant that identity a narrowly scoped TrueNAS NFSv4 ACL with traverse/read access to `incoming`, read/write/create/delete-child access to `import/{processing,quarantine,reports,dats}` and `appdata/importer`, and read/write/create/delete-child access only to `library/{roms,bios}`. Do not apply recursive ownership or ACL changes to the entire dataset. `incoming` remains mounted read-only.

There is exactly one administrator configured via TrueNAS secrets; no user records or roles exist. All administrative requests require an authenticated signed session and a CSRF token.

### Before the live RomM test

The automated suite uses a mocked RomM HTTP client; it does not contact the configured RomM server. Before enabling `ROMM_API_TOKEN` in production, verify the installed `/openapi.json` and use a least-privilege token. The application checks for the documented scan endpoint before allowing a connection check. A live scan remains the final manual acceptance step.

## Development

Python 3.12 and Poetry 2 are required. Run poetry install, poetry run ruff format --check ., poetry run ruff check ., poetry run mypy src, and poetry run pytest.
