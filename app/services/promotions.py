from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.services.finance import money, quote_fresh


@dataclass(frozen=True)
class PromotionPolicy:
    enabled: bool = False
    mode: str = "time"
    interval_seconds: int = 3600
    check_seconds: int = 120
    threshold: int = 10
    cooldown_seconds: int = 600
    night_enabled: bool = False
    night_interval_seconds: int = 14400
    night_threshold: int = 25
    per_operation: str = "0"
    daily: str = "0"
    monthly: str = "0"
    max_operations_per_item: int = 0

    def __post_init__(self):
        if (
            self.mode not in {"time", "position"}
            or self.interval_seconds < 600
            or self.check_seconds < 120
            or self.cooldown_seconds < 600
        ):
            raise ValueError("Недопустимые интервалы продвижения")
        if self.threshold < 1 or self.night_threshold < 1 or self.max_operations_per_item < 0:
            raise ValueError("Недопустимые ограничения")
        for value in (self.per_operation, self.daily, self.monthly):
            money(value)

    def due(self, at, last_bump, positions=()):
        if not self.enabled or at.tzinfo is None:
            return False
        local = at.astimezone(ZoneInfo("Europe/Moscow"))
        night = self.night_enabled and (local.hour >= 23 or local.hour < 8)
        elapsed = (at - last_bump).total_seconds() if last_bump else float("inf")
        if elapsed < self.cooldown_seconds:
            return False
        if self.mode == "time":
            return elapsed >= (self.night_interval_seconds if night else self.interval_seconds)
        if len(positions) < 2:
            return False
        first, second = positions[-2:]
        threshold = self.night_threshold if night else self.threshold
        return (
            all(isinstance(p[0], int) and p[0] > threshold for p in (first, second))
            and 0 <= (at - second[1]).total_seconds() <= self.check_seconds
            and self.check_seconds <= (second[1] - first[1]).total_seconds() <= self.check_seconds * 2
        )


def dry_run(store, item_id, quote, policy, at):
    """Budget evaluation only; this module cannot execute or charge a promotion."""
    reasons = []
    if not policy.enabled:
        reasons.append("автоподнятие выключено")
    if not quote_fresh(quote.get("checked_at")) or quote.get("price") is None:
        reasons.append("нет актуальной стоимости")
    price = money(quote["price"]) if quote.get("price") is not None else None
    local = at.astimezone(ZoneInfo("Europe/Moscow"))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    daily = Decimal(0)
    monthly = Decimal(0)
    count = 0
    for expense in store.rows(
        "SELECT * FROM promotion_expenses WHERE status IN ('RESERVED','SUCCEEDED','UNCERTAIN')"
    ):
        timestamp = datetime.fromisoformat(expense["created_at"])
        amount = money(expense["amount"])
        if timestamp >= month:
            monthly += amount
        if timestamp >= day:
            daily += amount
        if expense["item_id"] == item_id:
            count += 1
    if price is not None:
        if price > money(policy.per_operation):
            reasons.append("лимит на одну операцию")
        if daily + price > money(policy.daily):
            reasons.append("дневной лимит")
        if monthly + price > money(policy.monthly):
            reasons.append("месячный лимит")
        if count >= policy.max_operations_per_item:
            reasons.append("лимит операций на лот")
    return {
        "dry_run": True,
        "would_allow": not reasons,
        "reasons": reasons,
        "price": str(price) if price is not None else None,
        "charged": False,
    }
