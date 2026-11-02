"""One vocabulary for the product card, settings and /status."""

from app.telegram.formatting import escaped
from app.telegram.presentation import local_time

MODES = {
    "telegram_gifts": "Telegram Gifts",
    "unique_stock": "Уникальные данные со склада",
    "fixed_text": "Постоянный текст",
    "playerok": "📦 Playerok",
    "manual": "Ручная выдача",
    "unconfigured": "Не настроена — ожидается сверка привязки",
}
STATES = {
    "READY": "🟢 Готово к новым подходящим покупкам",
    "BLOCKED": "🟡 Включено, есть блокировка",
    "UNKNOWN": "⚪ Требуется актуальная проверка",
    "OFF": "🔴 Выключено владельцем — выдача остановлена",
    "STOPPED": "⛔ Остановлено — требуется решение",
}


def summary(result, *, compact=False):
    text = "Способ выдачи: " + MODES[result["mode"]] + "\n" + STATES[result["state"]]
    if result["mode"] == "manual":
        text = "Выдача: вручную\nАвтоматическая выдача не настроена."
    problems = result["problems"]
    if problems:
        text += f"\n{'Перед включением' if result['state'] == 'OFF' else 'Требует внимания'}: {len(problems)}"
        text += "\n" + "\n".join(
            "• " + escaped(p["text"], 130) for p in (problems[:1] if compact else problems)
        )
    elif result["state"] == "READY":
        text += "\nСохранённые разрешения, привязка, синхронизация и данные проверены."
    if not compact:
        if result.get("stock"):
            text += f"\nНа складе: {result['stock']['AVAILABLE']} доступно · {result['stock']['RESERVED']} зарезервировано"
        if result["required"] is not None:
            text += f"\nБаланс: {result['balance'].get('stars', 'не проверен')} ⭐ · пакет: {result['required']} ⭐ · резервы: {result['reserved']} ⭐"
            if result["shortage"]:
                text += f"\nНе хватает: {result['shortage']} ⭐"
            if result["balance"].get("at"):
                text += "\nЗамер Stars: " + local_time(result["balance"]["at"])
        text += "\n💬 " + escaped(result["message"], 180)
        text += "\n✅ Отметка выполнения: " + (
            result.get("completion_note")
            or ("автоматически, если Playerok разрешает без отчёта" if result["complete"] else "вручную")
        )
        text += "\nПолная проверка: " + (
            local_time(result["checked_at"]) if result["checked_at"] else "ещё не выполнялась"
        )
        text += "\nПолучатель и текущий заказ проверяются отдельно перед каждой выдачей."
    return text


def actions(result):
    rows, seen = [], set()
    for problem in result["problems"]:
        label, route = problem["action"]
        if route not in seen:
            seen.add(route)
            rows.append([(label, route)])
    return rows
