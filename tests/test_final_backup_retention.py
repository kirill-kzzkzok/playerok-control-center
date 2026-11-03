"""Automatic snapshots may be pruned only after read-only restoration checks."""

import os
import sqlite3
import time

from app.database.store import Store


def test_only_verified_old_startup_backups_are_pruned_and_latest_restores(tmp_path):
    from app.services.backup_retention import prune_startup_backups

    store = Store(tmp_path / "source.sqlite")
    directory = tmp_path / "backups"
    for i in range(14):
        path = directory / f"startup-20261101T1200{i:02}.sqlite3"
        store.backup(path)
        os.utime(path, (time.time() - 120, time.time() - 120))
    manual = directory / "manual.sqlite3"
    store.backup(manual)
    corrupt = directory / "startup-20261101T120059.sqlite3"
    corrupt.write_bytes(b"incomplete backup")
    recent = directory / "startup-20261103T120000.sqlite3"
    store.backup(recent)
    result = prune_startup_backups(directory, protected=recent)
    assert result["deleted"] == 4 and result["verified_kept"] == 11
    assert manual.exists() and corrupt.exists() and recent.exists()
    with sqlite3.connect(recent.as_uri() + "?mode=ro", uri=True) as restored:
        assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] > 0
    store.close()


def test_last_good_copy_and_linked_directory_are_never_deleted(tmp_path):
    from app.services.backup_retention import prune_startup_backups

    store = Store(tmp_path / "source.sqlite")
    directory = tmp_path / "backups"
    good = directory / "startup-20261101T120000.sqlite3"
    store.backup(good)
    os.utime(good, (time.time() - 120, time.time() - 120))
    assert prune_startup_backups(directory)["deleted"] == 0 and good.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = directory / "startup-20261101T120001.sqlite3"
    try:
        link.symlink_to(good)
    except OSError:
        pass  # Windows may not grant symlink creation; file safety is checked where supported.
    else:
        assert prune_startup_backups(directory)["deleted"] == 0 and link.is_symlink()
        directory_link = outside / "linked-backups"
        directory_link.symlink_to(directory, target_is_directory=True)
        assert prune_startup_backups(directory_link)["status"] == "UNTRUSTED_DIRECTORY"
    store.close()


def test_unverified_new_copy_blocks_cleanup_and_startup_records_result(tmp_path):
    from app.services.backup_retention import prune_startup_backups, startup_backup

    store = Store(tmp_path / "source.sqlite")
    directory = tmp_path / "backups"
    good = directory / "startup-20261101T120000.sqlite3"
    store.backup(good)
    invalid = directory / "startup-20261101T120001.sqlite3"
    invalid.write_bytes(b"not finished")
    assert prune_startup_backups(directory, protected=invalid)["status"] == "NEW_BACKUP_UNVERIFIED"
    assert good.exists()
    result = startup_backup(store, directory)
    assert result["status"] == "OK" and store.setting("backups:retention")["at"]
    assert store.rows("PRAGMA quick_check") == [{"quick_check": "ok"}]
    store.close()
