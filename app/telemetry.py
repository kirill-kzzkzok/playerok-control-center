"""Payload-free per-action timings; diagnostics never authorize execution."""

import logging
import time
from contextvars import ContextVar

measurements = ContextVar("ui_measurements", default=None)
LABELS = {
    "ack": "подтверждение кнопки",
    "fsm": "ожидание предыдущего действия",
    "navigation_lock": "ожидание панели",
    "sqlite": "чтение базы",
    "playerok_lock": "ожидание Playerok",
    "playerok": "запрос Playerok",
    "telegram": "обновление Telegram",
}


def record(stage, seconds):
    current = measurements.get()
    if current is not None:
        current[stage] = current.get(stage, 0) + max(0, seconds)


def finish(started, values):
    elapsed = time.perf_counter() - started
    logging.getLogger(__name__).debug("UI timing total=%.3f stages=%s", elapsed, values)
    if elapsed >= 3:
        from app.security.logging import event

        stage = max(values, key=values.get) if values else None
        name = LABELS.get(stage, "подготовка интерфейса")
        event("WARNING", f"Медленный отклик кнопки · {elapsed:.1f} с · {name}", level=logging.WARNING)
