"""Conservative retention for completed automatic SQLite startup snapshots only."""

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

AUTOMATIC = re.compile(r"startup-(\d{8})T\d{6}(?:\d{6})?\.sqlite3\Z")


def linked(path):
    return path.is_symlink() or path.is_junction()


def restorable(path):
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        ) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"schema_migrations", "outbox", "orders", "inventory"} <= tables and db.execute(
                "PRAGMA quick_check"
            ).fetchall() == [("ok",)]
    except (sqlite3.Error, OSError):
        return False


def kept_snapshots(entries):
    ordered = sorted(entries, key=lambda entry: entry[0].name, reverse=True)
    keep = {entry[0] for entry in ordered[:10]}
    months = set()
    for path, _, month in ordered:
        if month not in months and len(months) < 3:
            keep.add(path)
            months.add(month)
    return keep


def prune_startup_backups(directory, *, protected=None):
    root = Path(directory).absolute()
    result = {"deleted": 0, "verified_kept": 0, "skipped": 0, "status": "OK"}
    if any(linked(parent) for parent in (root, *root.parents)) or not root.is_dir():
        return {**result, "status": "UNTRUSTED_DIRECTORY"}
    resolved = root.resolve()
    protected = Path(protected).absolute() if protected else None
    if protected and (protected.parent != root or linked(protected) or not restorable(protected)):
        return {**result, "status": "NEW_BACKUP_UNVERIFIED"}
    entries, old = [], []
    for path in root.iterdir():
        match = AUTOMATIC.fullmatch(path.name)
        if not match or linked(path) or not path.is_file() or path.resolve().parent != resolved:
            result["skipped"] += 1
            continue
        stat = path.stat()
        if Path(str(path) + "-wal").exists() or Path(str(path) + "-journal").exists():
            result["skipped"] += 1
            continue
        entry = (path, stat, match.group(1)[:6])
        entries.append(entry)
        if path != protected and time.time() - stat.st_mtime >= 60:
            old.append(entry)
    # If metadata alone retains every file, deletion is impossible even if
    # some old snapshots are corrupt. Only the new snapshot needs verification.
    # Never reuse that shortcut once any deletion could be considered.
    if len(kept_snapshots(old)) == len(old):
        result["verified_kept"] = sum(path == protected for path, _, _ in entries)
        result["skipped"] += len(entries) - result["verified_kept"]
        return result
    good, untouched = [], []
    for path, stat, month in entries:
        if not restorable(path):
            result["skipped"] += 1
            continue
        if (
            path == protected
            or time.time() - stat.st_mtime < 60
            or Path(str(path) + "-wal").exists()
            or Path(str(path) + "-journal").exists()
        ):
            untouched.append(path)
        else:
            good.append((path, stat, month))
    keep = kept_snapshots(good)
    result["verified_kept"] = len(keep) + len(untouched)
    for path, original, _ in good:
        if path in keep:
            continue
        try:
            current = path.stat()
            if (
                linked(root)
                or root.resolve() != resolved
                or linked(path)
                or path.resolve().parent != resolved
                or (current.st_ino, current.st_size, current.st_mtime_ns)
                != (original.st_ino, original.st_size, original.st_mtime_ns)
            ):
                result["skipped"] += 1
                continue
            path.unlink()
            result["deleted"] += 1
        except OSError:
            result["skipped"] += 1
    return result


def startup_backup(store, directory):
    from datetime import UTC, datetime

    directory = Path(directory)
    if any(linked(parent) for parent in (directory.absolute(), *directory.absolute().parents)):
        raise ValueError("Каталог резервных копий содержит ссылку")
    destination = directory / (datetime.now(UTC).strftime("startup-%Y%m%dT%H%M%S%f") + ".sqlite3")
    store.backup(destination)
    result = prune_startup_backups(directory, protected=destination)
    store.set_setting("backups:retention", {**result, "at": datetime.now(UTC).isoformat()})
    return result


def attempt_startup_backup(store, directory):
    """Optional snapshot availability must not stop a healthy writable journal."""
    from app.database.store import now
    from app.security.logging import event

    previous = store.setting("backups:retention", {})
    try:
        result = startup_backup(store, directory)
    except (OSError, ValueError, sqlite3.Error) as error:
        # Never continue on a corrupt source, or swallow a failure to write the
        # durable health record (including a full source volume).
        if [tuple(row.values()) for row in store.rows("PRAGMA quick_check")] != [("ok",)]:
            raise
        result = {"status": "ERROR", "code": type(error).__name__, "at": now()}
        store.set_setting("backups:retention", result)
        if previous.get("status") != "ERROR":
            event("WARNING", "Резервная копия недоступна; бот повторит создание автоматически", level=30)
    else:
        if previous.get("status") == "ERROR":
            event("SUCCESS", "Создание резервных копий восстановлено")
    return result
