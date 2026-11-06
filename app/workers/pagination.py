"""Bounded opaque-cursor passes with a durable checkpoint and head refresh."""

import json

from app.database.store import Conflict, encode, now
from app.playerok.adapter import APIError, validate_page


class PageScanner:
    def __init__(self, store, api, *, pages_per_stream=4):
        if pages_per_stream < 1:
            raise ValueError("Лимит страниц должен быть положительным")
        self.store, self.api, self.pages_per_stream = store, api, pages_per_stream

    async def scan(self, key, method, collection, ingest, *, page_guard=None, on_entry_error=None, **kwargs):
        """Poll the head, then resume a bounded history pass. Never infer cursor ordering.

        Entries commit before the checkpoint: interruption replays at most a page,
        and durable inbox keys deduplicate it. Completed passes restart from the head
        next cycle, reconciling changes behind opaque/offset cursors as well.
        """
        rows = self.store.rows("SELECT cursor FROM sync_cursors WHERE key=?", (key,))
        checkpoint = json.loads(rows[0]["cursor"]) if rows and rows[0]["cursor"] else {}
        cursor, seen = checkpoint.get("after"), checkpoint.get("seen", [])
        count = 0

        async def fetch(after, visited):
            page = await self.api.call(method, after_cursor=after, **kwargs)
            entries, following = validate_page(
                page, collection, visited, validate_entries=on_entry_error is None
            )
            if page_guard:
                page_guard(page)
            for entry in entries:
                if on_entry_error is None:
                    ingest(entry)
                    continue
                try:
                    if not isinstance(entry, dict) or not entry.get("id"):
                        raise ValueError("Incomplete entry identity")
                    ingest(entry)
                except (ValueError, KeyError, TypeError, Conflict) as error:
                    on_entry_error(entry, error)
            return len(entries), following

        try:
            if cursor is not None:
                head_count, _ = await fetch(None, ())
                count += head_count
            for _ in range(self.pages_per_stream):
                size, following = await fetch(cursor, seen)
                count += size
                if following is not None:
                    seen.append(following)
                    if len(seen) > 10000:
                        raise APIError("PAGINATION_PASS_LIMIT")
                state = {"after": following, "seen": seen if following else []}
                self.store.execute(
                    "INSERT INTO sync_cursors VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                    (key, encode(state), now()),
                )
                if following is None:
                    self.store.set_setting(key + ":completed_at", now())
                    return count, True
                cursor = following
        except APIError as error:
            # A cursor may expire or change meaning. A new pass reconciles from head;
            # already committed records remain durable and are deduplicated.
            if error.code in {
                "INVALID_PAGINATION_CURSOR",
                "PAGINATION_PASS_LIMIT",
                "GRAPHQL_ERROR_OR_INCOMPLETE",
                "HTTP_400",
            }:
                self.store.execute("DELETE FROM sync_cursors WHERE key=?", (key,))
            raise
        return count, False
