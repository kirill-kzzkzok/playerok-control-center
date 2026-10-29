import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    playerok_header: str = field(repr=False)
    owner_ids: frozenset[int] = frozenset()
    telegram_proxy: str = ""
    use_proxy: bool = False
    writes_enabled: bool = False
    database_path: Path = Path("data/control_center.sqlite3")
    first_start_claim: bool = False
    realtime_enabled: bool = True
    automation_enabled: bool = False

    @classmethod
    def load(cls, path: str = ".env"):
        import os

        values = {**dotenv_values(path, interpolate=False), **os.environ}
        token = values.get("TELEGRAM_BOT_TOKEN", "") or ""
        header = values.get("PLAYEROK_HEADER_STRING", "") or ""
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigurationError("Некорректный TELEGRAM_BOT_TOKEN (значение скрыто)")
        if not header or "\n" in header or "\r" in header:
            raise ConfigurationError("PLAYEROK_HEADER_STRING отсутствует или содержит перевод строки")
        try:
            owners = frozenset(
                int(v.strip()) for v in (values.get("OWNER_IDS") or "").split(",") if v.strip()
            )
            if any(v <= 0 for v in owners):
                raise ValueError
        except ValueError:
            raise ConfigurationError(
                "OWNER_IDS должен содержать положительные Telegram ID через запятую"
            ) from None
        proxy = (values.get("TELEGRAM_API_PROXY_URL") or "").rstrip("/")
        use_proxy = values.get("TELEGRAM_USE_PROXY", "false").lower() == "true"
        if use_proxy and (not proxy.startswith("https://") or "@" in proxy):
            raise ConfigurationError("Прокси должен использовать HTTPS без credentials в URL")
        if values.get("PAID_OPERATIONS_ENABLED", "false").lower() == "true":
            raise ConfigurationError("Платные операции в этой сборке запрещены")
        return cls(
            token,
            header,
            owners,
            proxy,
            use_proxy,
            values.get("PLAYEROK_WRITES_ENABLED", "false").lower() == "true",
            Path(values.get("DATABASE_PATH") or "data/control_center.sqlite3"),
            values.get("FIRST_START_CLAIM", "false").lower() == "true",
            values.get("PLAYEROK_REALTIME_ENABLED", "true").lower() == "true",
            values.get("PLAYEROK_AUTOMATION_ENABLED", "false").lower() == "true",
        )

    def require_owner(self):
        if not self.owner_ids and not self.first_start_claim:
            raise ConfigurationError("Задайте OWNER_IDS в локальном .env. Доступ по /start не выдаётся.")
