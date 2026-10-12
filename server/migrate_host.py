"""Move Front Desk's data to a new host (see docs/HOSTING.md).

    python migrate_host.py pack                      what would be packed (changes nothing)
    python migrate_host.py pack --apply              write the archive
    python migrate_host.py unpack ARCHIVE            check the archive (changes nothing)
    python migrate_host.py unpack ARCHIVE --apply    install its database into bot/data

`pack` takes a consistent copy of bot/data/front_desk.db with SQLite's online backup API
(safe while the bot is running, WAL included), checks it, and writes one .tar.gz holding
exactly two files: front_desk.db and manifest.json (size, SHA-256, schema version). Nothing
else is ever packed: not server/.env, not logs, not the lock file, not the heartbeat.
Secrets travel separately (copy server/.env by hand).

`unpack` verifies the archive (only those two plain files, size cap, checksum, SQLite
integrity check) and installs the database. It refuses while a bot holds bot/data/bot.lock,
and refuses to replace an existing database unless --force is given; with --force the old
database is renamed, never deleted.

The archive contains member data: keep it out of git and delete it after the move. By default
it is written to bot/data/migrate/, which is gitignored.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import sqlite3
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
DATA_DIR = SERVER_DIR.parent / "bot" / "data"

DB_NAME = "front_desk.db"
MANIFEST_NAME = "manifest.json"
LOCK_NAME = "bot.lock"
FORMAT = 1
ARCHIVE_PREFIX = "front-desk-migrate-"
MEMBERS = (MANIFEST_NAME, DB_NAME)  # the only names an archive may contain
MAX_DB_BYTES = 2 * 1024**3  # refuse anything bigger: this bot's database is a few MB
MAX_MANIFEST_BYTES = 64 * 1024
CHUNK = 1024 * 1024


class MigrateError(Exception):
    """A problem worth showing to the person running the script (no traceback)."""


@dataclass(frozen=True)
class Paths:
    data: Path

    @property
    def db(self) -> Path:
        return self.data / DB_NAME

    @property
    def lock(self) -> Path:
        return self.data / LOCK_NAME

    @property
    def out(self) -> Path:
        return self.data / "migrate"


# ---------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def inspect_db(path: Path) -> dict:
    """Integrity check plus the facts the manifest records. Raises MigrateError if unhealthy."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            (tables,) = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise MigrateError(f"{path.name} is not a readable SQLite database ({e}).") from None
    if not result or result[0] != "ok":
        raise MigrateError(f"{path.name} failed SQLite's integrity check.")
    return {"schema_version": int(version), "tables": int(tables)}


def snapshot_db(src: Path, dest: Path) -> None:
    """Consistent copy of a live database (online backup API), as one standalone file."""
    if not src.is_file():
        raise MigrateError(f"No database at {src}.")
    dest.unlink(missing_ok=True)
    try:
        source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            target = sqlite3.connect(str(dest))
            try:
                source.backup(target)
                target.execute("PRAGMA journal_mode = DELETE")  # no -wal/-shm siblings needed
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error as e:
        dest.unlink(missing_ok=True)
        raise MigrateError(f"Couldn't copy {src.name} ({e}).") from None


def bot_running(lock: Path) -> bool:
    """True when another process holds the bot's single-instance lock (same call as main.py)."""
    if not lock.exists():
        return False
    try:
        handle = open(lock, "a+")
    except OSError:
        return True  # can't even open it: assume it is in use
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def archive_name(now: float) -> str:
    return ARCHIVE_PREFIX + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + ".tar.gz"


def build_manifest(db_file: Path, now: float) -> dict:
    return {
        "format": FORMAT,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source_os": platform.system(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "db": {"name": DB_NAME, "size": db_file.stat().st_size, "sha256": sha256_file(db_file),
               **inspect_db(db_file)},
        "not_included": ["server/.env", "logs", "lock file", "heartbeat", "nightly backups"],
    }


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(mtime)
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------- pack
def pack(paths: Paths, out_dir: Path | None = None, apply: bool = False, now: float | None = None,
         say=print) -> Path | None:
    """Write the migration archive; returns its path (None on a dry run)."""
    now = time.time() if now is None else now
    out_dir = Path(out_dir) if out_dir is not None else paths.out
    target = out_dir / archive_name(now)
    if not paths.db.is_file():
        raise MigrateError(f"No database at {paths.db}. Run this on the machine the bot runs on.")
    if not apply:
        say(f"DRY RUN. Would pack a consistent copy of {paths.db} ({fmt_bytes(paths.db.stat().st_size)} "
            f"on disk, plus anything still in its WAL) and a manifest into:")
        say(f"  {target}")
        say("Never packed: server/.env, logs, the lock file, the heartbeat, nightly backups.")
        say("Run again with --apply to write it.")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / (target.name + ".db.tmp")
    partial = out_dir / (target.name + ".tmp")
    try:
        snapshot_db(paths.db, work)
        manifest = build_manifest(work, now)
        with tarfile.open(partial, "w:gz") as tar:
            _add_bytes(tar, MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True).encode(), now)
            info = tarfile.TarInfo(DB_NAME)
            info.size = work.stat().st_size
            info.mtime = int(now)
            info.mode = 0o600
            with open(work, "rb") as f:
                tar.addfile(info, f)
        os.replace(partial, target)
    finally:
        work.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    db = manifest["db"]
    say(f"Packed {target} ({fmt_bytes(target.stat().st_size)}).")
    say(f"  database: {fmt_bytes(db['size'])}, schema v{db['schema_version']}, {db['tables']} tables, "
        f"sha256 {db['sha256'][:16]}...")
    say("It holds member data: copy it straight to the new host, keep it out of git, delete it afterwards.")
    say("server/.env is NOT inside. Copy that file separately.")
    return target


# ---------------------------------------------------------------- unpack
def read_archive(archive: Path, work: Path) -> dict:
    """Validate `archive` and extract its database to `work`. Returns the manifest."""
    if not archive.is_file():
        raise MigrateError(f"No archive at {archive}.")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            names = [m.name for m in members]
            if sorted(names) != sorted(MEMBERS):
                raise MigrateError(f"Not a Front Desk migration archive: expected exactly {', '.join(MEMBERS)}.")
            by_name = {m.name: m for m in members}
            if not all(m.isreg() for m in members):
                raise MigrateError("The archive contains something that is not a plain file.")
            if by_name[MANIFEST_NAME].size > MAX_MANIFEST_BYTES or by_name[DB_NAME].size > MAX_DB_BYTES:
                raise MigrateError("The archive is larger than a Front Desk database should be.")
            try:
                manifest = json.loads(tar.extractfile(by_name[MANIFEST_NAME]).read().decode("utf-8"))
                expected = manifest["db"]
                size, digest = int(expected["size"]), str(expected["sha256"])
                fmt = int(manifest["format"])
            except (ValueError, KeyError, TypeError):
                raise MigrateError("The archive's manifest is unreadable.") from None
            if fmt != FORMAT:
                raise MigrateError(f"Archive format {fmt} is not supported by this script (format {FORMAT}).")
            source = tar.extractfile(by_name[DB_NAME])
            hasher, written = hashlib.sha256(), 0
            work.unlink(missing_ok=True)
            with open(work, "wb") as out:  # never tar.extract(): the name on disk is ours
                while block := source.read(CHUNK):
                    written += len(block)
                    if written > MAX_DB_BYTES:
                        raise MigrateError("The archive is larger than a Front Desk database should be.")
                    hasher.update(block)
                    out.write(block)
    except (tarfile.TarError, EOFError, OSError) as e:
        work.unlink(missing_ok=True)
        raise MigrateError(f"Couldn't read {archive.name} ({type(e).__name__}). Copy it again.") from None
    except MigrateError:
        work.unlink(missing_ok=True)
        raise
    if written != size or hasher.hexdigest() != digest:
        work.unlink(missing_ok=True)
        raise MigrateError("The database doesn't match the manifest's checksum. Copy the archive again.")
    try:
        manifest["db"].update(inspect_db(work))
    except MigrateError:
        work.unlink(missing_ok=True)
        raise
    return manifest


def unpack(paths: Paths, archive: Path, apply: bool = False, force: bool = False,
           now: float | None = None, say=print) -> Path | None:
    """Install the archived database into the data dir; returns its path (None on a dry run)."""
    now = time.time() if now is None else now
    archive = Path(archive)
    if apply and bot_running(paths.lock):
        raise MigrateError("A bot is running here (bot.lock is held). Stop it first, then unpack.")
    scratch = None
    if apply:
        paths.data.mkdir(parents=True, exist_ok=True)
        work_dir = paths.data  # same filesystem as the destination: the final step is one rename
    else:
        scratch = tempfile.TemporaryDirectory()  # a dry run never writes into the data dir
        work_dir = Path(scratch.name)
    work = work_dir / (DB_NAME + ".incoming")
    try:
        manifest = read_archive(archive, work)
        db = manifest["db"]
        say(f"Archive OK: made {manifest.get('created_utc', '?')} on {manifest.get('source_os', '?')}; "
            f"database {fmt_bytes(db['size'])}, schema v{db['schema_version']}, {db['tables']} tables.")
        exists = paths.db.exists()
        if not apply:
            say(f"DRY RUN. Would install it as {paths.db}.")
            if exists:
                say("A database is already there: --force is needed, and the old one would be renamed, not deleted.")
            if bot_running(paths.lock):
                say("A bot is running here (bot.lock is held): stop it before --apply.")
            say("Run again with --apply to install it.")
            return None
        if exists and not force:
            raise MigrateError(f"{paths.db} already exists. Use --force to replace it (the old one is "
                               "renamed, not deleted).")
        if exists:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
            kept = paths.data / f"{DB_NAME}.before-migrate-{stamp}"
            os.replace(paths.db, kept)
            say(f"Kept the old database as {kept.name}.")
        else:
            kept = None
        # A leftover WAL belongs to the old database: it must never sit next to the new one.
        for suffix in ("-wal", "-shm"):
            stale = paths.data / (DB_NAME + suffix)
            if stale.exists():
                if kept is not None:
                    os.replace(stale, kept.with_name(kept.name + suffix))
                else:
                    stale.unlink()
        os.replace(work, paths.db)
    finally:
        work.unlink(missing_ok=True)
        if scratch is not None:
            scratch.cleanup()
    say(f"Installed {paths.db}. Start the bot; it migrates the schema itself if the code is newer.")
    return paths.db


# ---------------------------------------------------------------- entry point
def main(argv=None, say=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="bot data directory (default: bot/data)")
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("pack", help="write the migration archive (old host)")
    p.add_argument("--out", type=Path, default=None, help="directory for the archive (default: bot/data/migrate)")
    p.add_argument("--apply", action="store_true", help="actually write it (default: dry run)")
    u = sub.add_parser("unpack", help="install the archive's database (new host)")
    u.add_argument("archive", type=Path)
    u.add_argument("--apply", action="store_true", help="actually install it (default: dry run)")
    u.add_argument("--force", action="store_true", help="replace an existing database (the old one is renamed)")
    args = parser.parse_args(argv)
    paths = Paths(data=args.data_dir)
    try:
        if args.mode == "pack":
            pack(paths, out_dir=args.out, apply=args.apply, say=say)
        else:
            unpack(paths, args.archive, apply=args.apply, force=args.force, say=say)
    except MigrateError as e:
        say(f"Stopped: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
