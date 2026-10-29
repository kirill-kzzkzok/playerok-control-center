import asyncio
import json
import logging
import time

from app.database.store import now
from app.services.attachments import Attachments
from app.services.products import ValidationError


def referenced_photos(value):
    """Read explicit upload fields only; arbitrary strings never become delete targets."""
    paths = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"photos", "add_photos"} and isinstance(child, list):
                paths.extend(source for source in child if isinstance(source, str))
            else:
                paths.extend(referenced_photos(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(referenced_photos(child))
    return paths


class UploadCleanup:
    def __init__(self, store, attachments=None, grace_seconds=86400):
        self.store = store
        self.attachments = attachments or Attachments()
        self.grace_seconds = grace_seconds

    def once(self):
        active = set()
        successful = set()
        # Collect a consistent reference snapshot. A malformed payload aborts cleanup,
        # rather than silently treating live files as unreferenced.
        with self.store.transaction() as db:
            for row in db.execute("SELECT payload FROM drafts WHERE state IN ('DRAFT','QUEUED')"):
                active.update(referenced_photos(json.loads(row[0])))
            for row in db.execute("SELECT data FROM fsm"):
                active.update(referenced_photos(json.loads(row[0])))
            for row in db.execute("SELECT kind,target,payload,status FROM outbox"):
                paths = referenced_photos(json.loads(row["payload"]))
                if row["kind"] == "create":
                    draft = db.execute("SELECT payload FROM drafts WHERE id=?", (row["target"],)).fetchone()
                    if draft:
                        paths.extend(referenced_photos(json.loads(draft[0])))
                if row["status"] == "SUCCEEDED":
                    successful.update(paths)
                elif row["status"] != "PERMANENT_FAILED":
                    active.update(paths)
        active = {self.attachments.path(source) for source in active}
        successful = {self.attachments.path(source) for source in successful}
        removed = 0
        cutoff = time.time() - self.grace_seconds
        for path in self.attachments.directory.glob("*.jpg"):
            resolved = self.attachments.path(path)
            if resolved not in active and (resolved in successful or path.stat().st_mtime < cutoff):
                self.attachments.discard(resolved)
                removed += 1
        result = {"status": "OK", "removed": removed, "protected": len(active), "at": now()}
        self.store.set_setting("uploads:cleanup", result)
        return result

    async def run(self, stop):
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.once)
            except (OSError, ValueError, ValidationError) as error:
                self.store.set_setting(
                    "uploads:cleanup", {"status": "ERROR", "code": type(error).__name__, "at": now()}
                )
                logging.getLogger(__name__).error("Upload cleanup failed: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3600)
            except TimeoutError:
                continue
