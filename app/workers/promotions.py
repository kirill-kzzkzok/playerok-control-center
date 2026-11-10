import asyncio
import json
import math
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.database.store import encode, now
from app.playerok.adapter import APIError, validate_page
from app.security.logging import event
from app.services.products import ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings
from app.services.statistics import parsed_time


class PromotionWorker:
    """Bounded evaluator; explicit current authority may queue paid work through Outbox."""

    def __init__(self, store, api, *, per_cycle=None):
        self.store, self.api, self.settings = store, api, PromotionSettings(store, api)
        self.per_cycle = per_cycle
        if per_cycle is not None and not 1 <= per_cycle <= 20:
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

    def claim(self, revision=None):
        with self.store.transaction() as db:
            current_revision, policy, selection = self.settings.get()
            if not policy.enabled or revision is not None and revision != current_revision:
                return None
            revision = current_revision
            row = db.execute(
                "SELECT s.item_id FROM promotion_schedule s LEFT JOIN promotion_targets t ON t.item_id=s.item_id "
                "WHERE (s.due_at<=? OR s.policy_revision!=?) AND (s.lease_until IS NULL OR s.lease_until<?) "
                "AND (t.disposition='include' OR (?='all' AND coalesce(t.disposition,'default')!='exclude')) "
                "ORDER BY s.due_at,s.item_id LIMIT 1",
                (time.time(), revision, time.time(), selection),
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
        from app.services.paid_promotions import PaidPromotions

        await PaidPromotions(self.store, self.api).reconcile_due()
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
        previous_health = self.store.setting("promotions:health", {})
        last_success = previous_health.get("last_success")
        await self.discover(revision, selection)
        processed = 0
        selected_count = self.store.rows(
            "SELECT COUNT(*) AS n FROM promotion_schedule s LEFT JOIN promotion_targets t ON t.item_id=s.item_id "
            "WHERE t.disposition='include' OR (?='all' AND coalesce(t.disposition,'default')!='exclude')",
            (selection,),
        )[0]["n"]
        capacity = self.per_cycle or (
            max(4, min(20, math.ceil(selected_count * 30 / policy.check_seconds)))
            if policy.mode == "position"
            else 4
        )
        for _ in range(capacity):
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
                        approval = self.store.setting("promotions:approved:" + ident, {})
                        choices = [
                            quote
                            for quote in quotes
                            if quote.get("type") == item.get("priority")
                            and quote.get("price") is not None
                            and (not approval or quote.get("id") == approval.get("quote_id"))
                        ]
                        if len(choices) != 1:
                            raise ValidationError("Однозначная стоимость текущего типа размещения недоступна")
                        result = await self.settings.evaluate(actor, ident, choices[0]["id"])
                        event(
                            "CHECK",
                            f"🔍 Автоподнятие — {str(item.get('name') or ident)[:150]} — позиция {result['position'] or 'не подтверждена'}"
                            + (
                                " — условия выполнены"
                                if result["would_allow"]
                                else " — " + "; ".join(result["reasons"])
                            ),
                            level=10,
                        )
                        from app.services.paid_promotions import PaidPromotions

                        paid = PaidPromotions(self.store, self.api)
                        if result["would_allow"] and paid.has_authority(ident, revision) and approval:
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
                if paid_attempt and not (
                    isinstance(caught, APIError)
                    and not caught.uncertain
                    and caught.code
                    in {
                        "NETWORK_ERROR",
                        "HTTP_500",
                        "HTTP_502",
                        "HTTP_503",
                        "HTTP_504",
                        "RATE_LIMITED",
                        "CONNECTION_NOT_VERIFIED",
                    }
                ):
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
                if isinstance(caught, APIError) and str(caught.retry_after or "").isdigit():
                    interval = max(interval, int(caught.retry_after))
            else:
                failures = 0
                if self.settings.selected(ident, selection):
                    last_success = now()
            self.store.execute(
                "UPDATE promotion_schedule SET due_at=?,lease_token=NULL,lease_until=NULL,failures=?,error_code=?,checked_at=?,policy_revision=? WHERE item_id=? AND lease_token=?",
                (time.time() + interval, failures, error, now(), revision, ident, token),
            )
            processed += 1
        failed = self.store.rows(
            "SELECT s.item_id,s.failures,s.due_at,s.error_code FROM promotion_schedule s "
            "LEFT JOIN promotion_targets t ON t.item_id=s.item_id "
            "WHERE s.policy_revision=? AND s.error_code IS NOT NULL "
            "AND (t.disposition='include' OR (?='all' AND coalesce(t.disposition,'default')!='exclude'))",
            (revision, selection),
        )
        discovery = self.store.setting("promotions:discovery", {}) if selection == "all" else {}
        discovery_failed = discovery.get("revision") == revision and bool(discovery.get("error_code"))
        degraded = bool(failed) or discovery_failed
        incident = previous_health.get("incident") or uuid.uuid4().hex if degraded else None
        retries = [row["due_at"] for row in failed]
        if discovery_failed:
            retries.append(discovery.get("next_at", 0))
        persistent = any(row["failures"] >= 3 for row in failed) or (
            discovery_failed and discovery.get("failures", 0) >= 3
        )
        current_revision, current, _ = self.settings.get()
        from app.services.paid_promotions import PaidPromotions

        paid = PaidPromotions(self.store, self.api)
        purchase_authority = any(
            paid.has_authority(row["item_id"], revision)
            for row in self.store.rows(
                "SELECT s.item_id FROM promotion_schedule s LEFT JOIN promotion_targets t ON t.item_id=s.item_id "
                "WHERE t.disposition='include' OR (?='all' AND coalesce(t.disposition,'default')!='exclude')",
                (selection,),
            )
        )
        if purchase_authority:
            try:
                paid.permissions(automatic=True)
            except ValidationError:
                purchase_authority = False
        status = (
            "DISABLED"
            if not current.enabled
            else "STALE"
            if current_revision != revision
            else "DEGRADED"
            if degraded
            else "SCHEDULED"
            if purchase_authority
            else "DRY_RUN"
        )
        if status == "DRY_RUN" and (
            previous_health.get("status") != status or previous_health.get("revision") != revision
        ):
            event(
                "INFO",
                "🚀 Автоподнятие проверяет позиции без покупок. "
                "Проверьте разрешение расходов для текущих настроек в разделе «Продвижение».",
            )
        summary_at = previous_health.get("summary_at")
        summary_time = parsed_time(summary_at)
        if processed and (not summary_time or time.time() - summary_time.timestamp() >= 600):
            summaries = []
            for row in self.store.rows(
                "SELECT s.item_id,s.error_code,e.result FROM promotion_schedule s "
                "LEFT JOIN promotion_targets t ON t.item_id=s.item_id "
                "LEFT JOIN promotion_evaluations e ON e.id=(SELECT MAX(id) FROM promotion_evaluations "
                "WHERE item_id=s.item_id AND policy_revision=?) "
                "WHERE t.disposition='include' OR (?='all' AND coalesce(t.disposition,'default')!='exclude') "
                "ORDER BY s.item_id",
                (revision, selection),
            ):
                result = json.loads(row["result"]) if row["result"] else {}
                name = (
                    result.get("item_name")
                    or self.store.setting("promotions:approved:" + row["item_id"], {}).get("name")
                    or "Товар"
                )
                label = re.search(r"\[[A-Za-z0-9]{3}\]$", name)
                label = label.group() if label else " ".join(name.split())[:35]
                reasons = result.get("reasons") or []
                if row["error_code"]:
                    reason = "чтение Playerok пока недоступно"
                elif not result:
                    reason = "ожидает проверки"
                elif not reasons:
                    continue  # Queuing is not a confirmed purchase; its receipt has a separate event.
                elif "Позиция в категории:" in reasons[0]:
                    reason = f"позиция {result.get('position')}, пропуск"
                elif "паузы" in reasons[0]:
                    reason = "пауза между покупками"
                elif any("лимит" in value for value in reasons):
                    reason = "достигнут лимит расходов"
                else:
                    reason = reasons[0]
                summaries.append(label + " — " + reason)
            if summaries:
                event("INFO", "🚀 Автоподнятие · " + " | ".join(summaries))
                summary_at = now()
        self.store.set_setting(
            "promotions:health",
            {
                "status": status,
                "at": now(),
                "processed": processed,
                "revision": revision,
                "failed_items": len(failed),
                "discovery_error": discovery.get("error_code") if discovery_failed else None,
                "error_codes": sorted(
                    {row["error_code"] for row in failed}
                    | ({discovery["error_code"]} if discovery_failed else set())
                ),
                "last_success": last_success,
                "next_retry": min(retries, default=None),
                "incident": incident,
                "summary_at": summary_at,
            },
        )
        if persistent and status == "DEGRADED":
            self.store.execute(
                "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'promotion_error',?,?,?)",
                (
                    f"promotion_error:{incident}",
                    encode({"failed_items": len(failed), "incident": incident}),
                    now(),
                    now(),
                ),
            )
        return processed

    async def run(self, stop):
        from app.workers.supervisor import worker_cycle

        while not stop.is_set():
            started = time.monotonic()
            try:
                with worker_cycle(self.store, "promotions", 30) as cycle:
                    cycle["processed"] = await self.once() or 0
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
                await asyncio.wait_for(stop.wait(), max(1, 5 - (time.monotonic() - started)))
            except TimeoutError:
                pass
