"""Isolated opt-in. Never turns on Playerok or gift spending by implication."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class GiftSettings:
    api_id: int
    api_hash: str = field(repr=False)
    session_path: str
    category_id: str
    obtaining_type_id: str
    buyer_username_field_id: str
    payment_enabled: bool
    max_order_stars: int
    max_daily_stars: int
    refund_enabled: bool = False
    max_gifts: int = 5
    telegram_transport: str = "legacy"

    @classmethod
    def load(cls, path: str = ".env") -> "GiftSettings":
        env = {**dotenv_values(path, interpolate=False), **os.environ}
        from app.localvpn import NetworkSettings

        def positive(name, default):
            value = int(env.get(name) or default)
            if value <= 0:
                raise ValueError(name + " must be positive")
            return value

        session = Path(env.get("TG_USER_SESSION_PATH") or "data/telegram_personal")
        if session.suffix != ".session":
            session = session.with_suffix(".session")
        if not session.resolve().is_relative_to(Path("data").resolve()):
            raise ValueError("Personal session must stay inside the ignored data/ directory")
        return cls(
            api_id=int(env.get("TG_USER_API_ID") or 0),
            api_hash=str(env.get("TG_USER_API_HASH") or ""),
            session_path=str(session),
            category_id=str(env.get("PLAYEROK_STARS_CATEGORY_ID") or ""),
            obtaining_type_id=str(env.get("PLAYEROK_STARS_GIFT_OBTAINING_ID") or ""),
            buyer_username_field_id=str(env.get("PLAYEROK_STARS_USERNAME_FIELD_ID") or ""),
            payment_enabled=str(env.get("TG_GIFT_PAYMENTS_ENABLED") or "false").lower() == "true",
            max_order_stars=positive("TG_GIFT_MAX_ORDER_STARS", "100"),
            max_daily_stars=positive("TG_GIFT_MAX_DAILY_STARS", "300"),
            refund_enabled=str(env.get("TG_GIFT_REFUNDS_ENABLED") or "false").lower() == "true",
            max_gifts=min(20, positive("TG_GIFT_MAX_COUNT", "5")),
            telegram_transport=NetworkSettings.load(path).mode,
        )

    def require_configured(self):
        if not (
            self.api_id > 0
            and self.api_hash
            and self.category_id
            and self.obtaining_type_id
            and self.buyer_username_field_id
        ):
            raise ValueError("Не указаны API ID/hash и подтверждённые ID категории/способа передачи")
