"""Tests for server/migrate_host.py. Everything happens in temp directories; no network."""

import hashlib
import io
import json
import sqlite3
import sys
import tarfile

import pytest

import migrate_host as M

NOW = 1_800_000_000


# ---------------------------------------------------------------- helpers
def make_db(path, rows=3, version=15, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS wallet (user_id INTEGER PRIMARY KEY, coins INTEGER)")
    conn.executemany("INSERT INTO wallet VALUES (?, ?)", [(i, i * 10) for i in range(1, rows + 1)])
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return conn


def rows_of(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT user_id, coins FROM wallet ORDER BY user_id").fetchall()
    finally:
        conn.close()


@pytest.fixture
def old(tmp_path):
    """The old host: a data dir with a database and the files that must never travel."""
    data = tmp_path / "old" / "bot" / "data"
    make_db(data / M.DB_NAME).close()
    (data / "bot.log").write_text("log line with a member name\n", encoding="utf-8")
    (data / "bot.lock").write_text("", encoding="utf-8")
    (data / "heartbeat").write_text('{"ts": 1}', encoding="utf-8")
    (data / "watchdog.json").write_text("{}", encoding="utf-8")
    (data / ".env").write_text("DISCORD_TOKEN=not-a-real-token\n", encoding="utf-8")
    (data / "backups").mkdir()
    (data / "backups" / "front_desk-2026-10-01.db").write_bytes(b"old backup")
    return M.Paths(data=data)


@pytest.fixture
def new(tmp_path):
    return M.Paths(data=tmp_path / "new" / "bot" / "data")


def quiet(*_):
    pass


def packed(old, tmp_path):
    return M.pack(old, out_dir=tmp_path / "out", apply=True, now=NOW, say=quiet)


def write_archive(path, files, link=None):
    """A hand-made archive: {name: bytes}, optionally with a symlink member."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return path


def manifest_for(db_bytes, **over):
    db = {"name": M.DB_NAME, "size": len(db_bytes), "sha256": hashlib.sha256(db_bytes).hexdigest()}
    return json.dumps({"format": M.FORMAT, "db": db, **over}).encode()


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---------------------------------------------------------------- pack
def test_pack_is_a_dry_run_by_default(old, tmp_path):
    before = tree(tmp_path)
    said = []
    assert M.pack(old, out_dir=tmp_path / "out", now=NOW, say=said.append) is None
    assert tree(tmp_path) == before
    assert "DRY RUN" in said[0] and any("--apply" in line for line in said)


def test_pack_writes_exactly_the_database_and_a_manifest(old, tmp_path):
    archive = packed(old, tmp_path)
    assert archive.name == "front-desk-migrate-20270115-080000.tar.gz"
    with tarfile.open(archive) as tar:
        assert sorted(tar.getnames()) == sorted(M.MEMBERS)
        assert all(m.isreg() for m in tar.getmembers())
        manifest = json.loads(tar.extractfile(M.MANIFEST_NAME).read())
        db_bytes = tar.extractfile(M.DB_NAME).read()
    assert manifest["format"] == M.FORMAT
    assert manifest["db"]["sha256"] == hashlib.sha256(db_bytes).hexdigest()
    assert manifest["db"]["size"] == len(db_bytes)
    assert manifest["db"]["schema_version"] == 15
    assert manifest["db"]["tables"] == 1
    assert manifest["created_utc"] == "2027-01-15T08:00:00Z"


def test_pack_never_includes_secrets_logs_or_the_lock(old, tmp_path):
    archive = packed(old, tmp_path)
    with tarfile.open(archive) as tar:
        blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers())
        names = tar.getnames()
    assert not any(n in names for n in (".env", "bot.log", "bot.lock", "heartbeat", "watchdog.json"))
    assert b"not-a-real-token" not in blob
    assert b"member name" not in blob
    assert b"old backup" not in blob


def test_pack_leaves_no_temp_files_and_does_not_touch_the_source(old, tmp_path):
    before = M.sha256_file(old.db)
    archive = packed(old, tmp_path)
    assert [p.name for p in (tmp_path / "out").iterdir()] == [archive.name]
    assert M.sha256_file(old.db) == before


def test_pack_defaults_to_the_gitignored_data_dir(old):
    archive = M.pack(old, apply=True, now=NOW, say=quiet)
    assert archive.parent == old.data / "migrate"


def test_pack_is_consistent_while_the_bot_is_writing(tmp_path, new):
    """WAL mode with an open writer: committed rows are packed, uncommitted ones are not."""
    data = tmp_path / "live"
    writer = make_db(data / M.DB_NAME, rows=5, wal=True)
    try:
        writer.execute("INSERT INTO wallet VALUES (99, 990)")  # not committed
        assert (data / (M.DB_NAME + "-wal")).exists()
        archive = M.pack(M.Paths(data=data), out_dir=tmp_path / "out", apply=True, now=NOW, say=quiet)
    finally:
        writer.rollback()
        writer.close()
    M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert rows_of(new.db) == [(i, i * 10) for i in range(1, 6)]
    assert not (new.data / (M.DB_NAME + "-wal")).exists()  # a standalone file


def test_pack_without_a_database_fails_cleanly(tmp_path):
    with pytest.raises(M.MigrateError, match="No database"):
        M.pack(M.Paths(data=tmp_path / "nothing"), apply=True, now=NOW, say=quiet)
    assert not (tmp_path / "nothing").exists()


def test_pack_refuses_a_corrupt_database(tmp_path):
    data = tmp_path / "bad"
    data.mkdir()
    (data / M.DB_NAME).write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(M.MigrateError):
        M.pack(M.Paths(data=data), out_dir=tmp_path / "out", apply=True, now=NOW, say=quiet)
    assert list((tmp_path / "out").iterdir()) == []


# ---------------------------------------------------------------- unpack
def test_unpack_is_a_dry_run_by_default(old, new, tmp_path):
    archive = packed(old, tmp_path)
    before = tree(tmp_path)
    said = []
    assert M.unpack(new, archive, now=NOW, say=said.append) is None
    assert tree(tmp_path) == before
    assert not new.data.exists()
    assert any("DRY RUN" in line for line in said)


def test_round_trip_restores_every_row(old, new, tmp_path):
    archive = packed(old, tmp_path)
    assert M.unpack(new, archive, apply=True, now=NOW, say=quiet) == new.db
    assert rows_of(new.db) == rows_of(old.db)
    assert M.inspect_db(new.db)["schema_version"] == 15
    assert [p.name for p in new.data.iterdir()] == [M.DB_NAME]  # nothing else arrives


def test_unpack_refuses_to_replace_a_database_without_force(old, new, tmp_path):
    archive = packed(old, tmp_path)
    make_db(new.db, rows=1).close()
    with pytest.raises(M.MigrateError, match="--force"):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert rows_of(new.db) == [(1, 10)]
    assert [p.name for p in new.data.iterdir()] == [M.DB_NAME]


def test_unpack_force_keeps_the_old_database_and_its_wal(old, new, tmp_path):
    archive = packed(old, tmp_path)
    make_db(new.db, rows=1).close()
    (new.data / (M.DB_NAME + "-wal")).write_bytes(b"stale wal")
    (new.data / (M.DB_NAME + "-shm")).write_bytes(b"stale shm")
    M.unpack(new, archive, apply=True, force=True, now=NOW, say=quiet)
    kept = new.data / f"{M.DB_NAME}.before-migrate-20270115-080000"
    assert rows_of(new.db) == rows_of(old.db)
    assert kept.exists()
    assert (new.data / (kept.name + "-wal")).read_bytes() == b"stale wal"
    assert not (new.data / (M.DB_NAME + "-wal")).exists()
    assert not (new.data / (M.DB_NAME + "-shm")).exists()


def test_unpack_removes_an_orphan_wal_on_a_fresh_host(old, new, tmp_path):
    archive = packed(old, tmp_path)
    new.data.mkdir(parents=True)
    (new.data / (M.DB_NAME + "-wal")).write_bytes(b"orphan")
    M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert not (new.data / (M.DB_NAME + "-wal")).exists()
    assert rows_of(new.db) == rows_of(old.db)


def test_unpack_dry_run_mentions_an_existing_database(old, new, tmp_path):
    archive = packed(old, tmp_path)
    make_db(new.db, rows=1).close()
    said = []
    M.unpack(new, archive, now=NOW, say=said.append)
    assert any("--force" in line for line in said)
    assert rows_of(new.db) == [(1, 10)]


def test_unpack_refuses_while_the_bot_holds_the_lock(old, new, tmp_path):
    archive = packed(old, tmp_path)
    new.data.mkdir(parents=True)
    handle = open(new.lock, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert M.bot_running(new.lock)
        with pytest.raises(M.MigrateError, match="running"):
            M.unpack(new, archive, apply=True, now=NOW, say=quiet)
        assert not new.db.exists()
    finally:
        handle.close()
    assert not M.bot_running(new.lock)
    M.unpack(new, archive, apply=True, now=NOW, say=quiet)  # fine once it is stopped
    assert new.db.exists()


def test_bot_running_is_false_without_a_lock_file(tmp_path):
    assert not M.bot_running(tmp_path / "bot.lock")


def good_db_bytes(tmp_path):
    make_db(tmp_path / "src" / M.DB_NAME).close()
    return (tmp_path / "src" / M.DB_NAME).read_bytes()


@pytest.mark.parametrize("extra", ["../evil.txt", "/abs.txt", ".env", "sub/front_desk.db"])
def test_unpack_rejects_unexpected_members(new, tmp_path, extra):
    db = good_db_bytes(tmp_path)
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: manifest_for(db), M.DB_NAME: db, extra: b"x"})
    with pytest.raises(M.MigrateError, match="expected exactly"):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert not new.db.exists()
    assert not (tmp_path / "evil.txt").exists()


def test_unpack_rejects_a_renamed_database_path(new, tmp_path):
    db = good_db_bytes(tmp_path)
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: manifest_for(db), "../" + M.DB_NAME: db})
    with pytest.raises(M.MigrateError):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert not new.db.exists()


def test_unpack_rejects_links(new, tmp_path):
    db = good_db_bytes(tmp_path)
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: manifest_for(db)}, link=M.DB_NAME)
    with pytest.raises(M.MigrateError, match="plain file"):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert not new.db.exists()


def test_unpack_rejects_a_checksum_mismatch(new, tmp_path):
    db = good_db_bytes(tmp_path)
    tampered = db[:-1] + bytes([db[-1] ^ 1])
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: manifest_for(db), M.DB_NAME: tampered})
    with pytest.raises(M.MigrateError, match="checksum"):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert list(new.data.iterdir()) == []


def test_unpack_rejects_something_that_is_not_sqlite(new, tmp_path):
    junk = b"definitely not a database" * 50
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: manifest_for(junk), M.DB_NAME: junk})
    with pytest.raises(M.MigrateError):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert list(new.data.iterdir()) == []


@pytest.mark.parametrize("manifest", [b"not json", b"{}", b'{"format": 1, "db": {"size": "x", "sha256": 1}}'])
def test_unpack_rejects_a_bad_manifest(new, tmp_path, manifest):
    db = good_db_bytes(tmp_path)
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: manifest, M.DB_NAME: db})
    with pytest.raises(M.MigrateError, match="manifest"):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)


def test_unpack_rejects_an_unknown_format(new, tmp_path):
    db = good_db_bytes(tmp_path)
    manifest = json.loads(manifest_for(db))
    manifest["format"] = 99
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: json.dumps(manifest).encode(), M.DB_NAME: db})
    with pytest.raises(M.MigrateError, match="format 99"):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)


def test_unpack_rejects_an_oversized_database(new, tmp_path, monkeypatch):
    db = good_db_bytes(tmp_path)
    archive = write_archive(tmp_path / "a.tar.gz", {M.MANIFEST_NAME: manifest_for(db), M.DB_NAME: db})
    monkeypatch.setattr(M, "MAX_DB_BYTES", 1024)
    with pytest.raises(M.MigrateError, match="larger"):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert not new.db.exists()


@pytest.mark.parametrize("content", [b"", b"plain text, not gzip", b"\x1f\x8b\x08\x00truncated"])
def test_unpack_rejects_a_broken_file(new, tmp_path, content):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(content)
    with pytest.raises(M.MigrateError):
        M.unpack(new, archive, apply=True, now=NOW, say=quiet)
    assert not new.db.exists()


def test_unpack_missing_archive(new, tmp_path):
    with pytest.raises(M.MigrateError, match="No archive"):
        M.unpack(new, tmp_path / "nope.tar.gz", apply=True, now=NOW, say=quiet)


# ---------------------------------------------------------------- command line
def test_main_round_trip_and_exit_codes(old, new, tmp_path):
    said = []
    out = tmp_path / "out"
    assert M.main(["--data-dir", str(old.data), "pack", "--out", str(out)], say=said.append) == 0
    assert not out.exists()  # dry run
    assert M.main(["--data-dir", str(old.data), "pack", "--out", str(out), "--apply"], say=said.append) == 0
    (archive,) = out.iterdir()
    assert M.main(["--data-dir", str(new.data), "unpack", str(archive)], say=said.append) == 0
    assert not new.db.exists()  # dry run
    assert M.main(["--data-dir", str(new.data), "unpack", str(archive), "--apply"], say=said.append) == 0
    assert rows_of(new.db) == rows_of(old.db)
    assert M.main(["--data-dir", str(new.data), "unpack", str(archive), "--apply"], say=said.append) == 1
    assert said[-1].startswith("Stopped:")
    assert M.main(["--data-dir", str(new.data), "unpack", str(archive), "--apply", "--force"], say=said.append) == 0


def test_main_reports_a_missing_database(tmp_path):
    said = []
    assert M.main(["--data-dir", str(tmp_path / "none"), "pack", "--apply"], say=said.append) == 1
    assert "No database" in said[-1]


def test_main_requires_a_mode():
    with pytest.raises(SystemExit):
        M.main([])


def test_output_never_prints_the_environment(old, tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token-xyz")
    said = []
    M.main(["--data-dir", str(old.data), "pack", "--out", str(tmp_path / "out"), "--apply"], say=said.append)
    assert "not-a-real-token-xyz" not in "\n".join(said)
