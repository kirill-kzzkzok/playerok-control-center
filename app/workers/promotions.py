import asyncio
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.database.store import encode, now
from app.playerok.adapter import APIError, validate_page
from app.services.products import ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings


class PromotionWorker:
    """Bounded evaluator; explicit current authority may queue paid work through Outbox."""

    def __init__(self, store, api, *, per_cycle=4):
        self.store, self.api, self.settings = store, api, PromotionSettings(store, api)
        self.per_cycle = per_cycle
        if not 1 <= per_cycle <= 20:
            raise ValueError("Недопустимый лимит проверок")

    async def discover(self, revision, selection):
        if selection == "selected":
            self.store.execute(
                "INSERT OR IGNORE INTO promotion_schedule(item_id) SELECT item_id FROM promotion_targets WHERE disposition='include'"
            )
            return
        state = self.store.setting("promotions:discovery", {})
        if state.get("revision") != revision:
            state = {"revision": revision, "after": None, "seen": []}
        if state.get("next_at", 0) > time.time():
            return
        try:
            page = await self.api.call("get_my_items", count=24, after_cursor=state.get("after"))
        except APIError as error:
            state["failures"] = state.get("failures", 0) + 1
            delay = min(900, 30 * 2 ** min(state["failures"], 5))
            if str(error.retry_after or "").isdigit():
                delay = max(delay, int(error.retry_after))
            state.update(next_at=time.time() + delay, error_code=error.code)
            self.store.set_setting("promotions:discovery", state)
            return
        try:
            values, following = validate_page(page, "items", state.get("seen", []))
            seen = state.get("seen", []) + ([following] if following else [])
            if len(seen) > 10000:
                raise APIError("PAGINATION_PASS_LIMIT")
        except APIError:
            self.store.set_setting("promotions:discovery", {"revision": revision})
            raise
        # Entries before checkpoint: a crash replays safely through unique item IDs.
        for item in values:
            self.store.execute("INSERT OR IGNORE INTO promotion_schedule(item_id) VALUES(?)", (item["id"],))
        self.store.set_setting(
            "promotions:discovery",
            {
                "revision": revision,
                "after": following,
                "seen": seen if following else [],
                "next_at": 0 if following else time.time() + 300,
            },
        )

    def claim(self, revision=0):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT item_id FROM promotion_schedule WHERE (due_at<=? OR policy_revision!=?) AND (lease_until IS NULL OR lease_until<?) ORDER BY due_at,item_id LIMIT 1",
                (time.time(), revision, time.time()),
            ).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE promotion_schedule SET lease_token=?,lease_until=? WHERE item_id=?",
                (token, time.time() + 180, row[0]),
            )
            return row[0], token

    async def once(self):
        revision, policy, selection = self.settings.get()
        if not policy.enabled:
            self.store.set_setting("promotions:health", {"status": "DISABLED", "at": now()})
            return 0
        owners = self.store.rows(
            "SELECT actor FROM promotion_policy p JOIN administrators a ON a.user_id=p.actor WHERE p.id=1"
        )
        if not owners:
            self.store.set_setting("promotions:health", {"status": "OWNER_UNAVAILABLE", "at": now()})
            return 0
        actor = owners[0]["actor"]
        await self.discover(revision, selection)
        processed = 0
        for _ in range(self.per_cycle):
            current_revision, current, _ = self.settings.get()
            if current_revision != revision or not current.enabled:
                break
            claimed = self.claim(revision)
            if not claimed:
                break
            ident, token = claimed
            error = None
            paid_attempt = False
            interval = (
                policy.check_seconds if policy.mode == "position" else min(policy.interval_seconds, 300)
            )
            try:
                if self.settings.selected(ident, selection):
                    item = await self.settings.access.item(actor, ident)
                    if item.get("status") == "APPROVED":
                        quotes = await self.api.call(
                            "get_item_priority_statuses", item_id=ident, item_price=priority_price_basis(item)
                        )
                        # Evaluate the current placement class, never silently select a paid upgrade.
                        choices = [
                            quote
                            for quote in quotes
                            if quote.get("type") == item.get("priority") and quote.get("price") is not None
                        ]
                        if len(choices) != 1:
                            raise ValidationError("Однозначная стоимость текущего типа размещения недоступна")
                        result = await self.settings.evaluate(actor, ident, choices[0]["id"])
                        from app.services.paid_promotions import PaidPromotions

                        paid = PaidPromotions(self.store, self.api)
                        approval = self.store.setting("promotions:approved:" + ident, {})
                        authority = self.store.setting("promotions:paid_authority", {})
                        if result["would_allow"] and authority.get("revision") == revision and approval:
                            paid_attempt = True
                            preview = await paid.preview(actor, ident, approval["quote_id"])
                            await paid.queue(
                                actor, preview, f"promotion:auto:{ident}:{revision}:{token}", automatic=True
                            )
                        if result["would_allow"] and policy.mode == "time":
                            local_hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
                            start, end = policy.night_start_hour, policy.night_end_hour
                            night = policy.night_enabled and (
                                (local_hour >= start or local_hour < end)
                                if start > end
                                else start <= local_hour < end
                            )
                            interval = policy.night_interval_seconds if night else policy.interval_seconds
            except (APIError, ValueError, KeyError, TypeError) as caught:
                error = caught.code if isinstance(caught, APIError) else "PROMOTION_CHECK_UNAVAILABLE"
                if paid_attempt:
                    self.store.execute(
                        "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'promotion_guard',?,?,?)",
                        (
                            f"promotion_guard:paid:{revision}:{ident}:{error}",
                            encode(
                                {
                                    "item_id": ident,
                                    "revision": revision,
                                    "paid_schedule_block": True,
                                    "reasons": [
                                        caught.code if isinstance(caught, APIError) else str(caught)[:400]
                                    ],
                                }
                            ),
                            now(),
                            now(),
                        ),
                    )
                failures = (
                    self.store.rows("SELECT failures FROM promotion_schedule WHERE item_id=?", (ident,))[0][
                        "failures"
                    ]
                    + 1
                )
                interval = min(900, 30 * 2 ** min(failures, 5))
            else:
                failures = 0
            self.store.execute(
                "UPDATE promotion_schedule SET due_at=?,lease_token=NULL,lease_until=NULL,failures=?,error_code=?,checked_at=?,policy_revision=? WHERE item_id=? AND lease_token=?",
                (time.time() + interval, failures, error, now(), revision, ident, token),
            )
            processed += 1
        self.store.set_setting(
            "promotions:health",
            {
                "status": "SCHEDULED"
                if self.store.setting("promotions:paid_authority", {}).get("revision") == revision
                else "DRY_RUN",
                "at": now(),
                "processed": processed,
            },
        )
        return processed

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.once()
            except Exception as error:  # noqa: BLE001 -- keep read-only scheduler alive, log safe type/code only
                self.store.set_setting(
                    "promotions:health",
                    {
                        "status": "ERROR",
                        "code": error.code if isinstance(error, APIError) else type(error).__name__,
                        "at": now(),
                    },
                )
            try:
                await asyncio.wait_for(stop.wait(), 30)
            except TimeoutError:
                pass
