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
    "WAITING_PUBLICATION": "⏳ Выдача настроена. Ожидает публикации.",
    "READY": "🟢 Готово к новым подходящим покупкам",
    "BLOCKED": "🟡 Включено, есть блокировка",
    "UNKNOWN": "⚪ Требуется актуальная проверка",
    "OFF": "🔴 Выключено владельцем — выдача остановлена",
    "STOPPED": "⛔ Остановлено — требуется решение",
}
PLATFORM_STATES = {
    "WAITING_PUBLICATION": "⏳ Выдача настроена. Ожидает публикации.",
    "READY": "✅ Действия бота после оплаты готовы",
    "OFF": "⏸ Действия бота после оплаты выключены",
    "UNKNOWN": "🔄 Проверяем действия бота после оплаты",
    "BLOCKED": "⚠️ Действия бота после оплаты ожидают устранения проблемы",
    "STOPPED": "⛔ Действия бота после оплаты приостановлены",
}


def switch(label, enabled):
    return ("🟢 " if enabled else "🔴 ") + label


def summary(result, *, compact=False):
    text = "Способ выдачи: " + MODES[result["mode"]] + "\n" + STATES[result["state"]]
    if result["mode"] == "playerok":
        text = "📦 Товар выдаёт Playerok\n" + PLATFORM_STATES[result["state"]]
    if result["mode"] == "manual":
        text = "Выдача: вручную\nАвтоматическая выдача не настроена."
    problems = result["problems"]
    waiting = {"IN_PROGRESS", "GIFT_IN_PROGRESS"}
    if result["state"] == "UNKNOWN" and any(p["code"] == "GIFT_IN_PROGRESS" for p in problems):
        text = (
            "Способ выдачи: " + MODES[result["mode"]] + "\n⏳ Отправляем подарок, следующие покупки ожидают"
        )
    actionable = [p for p in problems if not p["unknown"] or p["code"] == "CHECK_FAILED"]
    if actionable:
        text += (
            f"\n{'Перед включением' if result['state'] == 'OFF' else 'Требует внимания'}: {len(actionable)}"
        )
    if any(p["unknown"] and p["code"] not in waiting | {"CHECK_FAILED"} for p in problems):
        text += (
            "\nПроверки актуальности обновляются автоматически."
            if result["enabled"]
            else "\nАктуальность проверяется перед включением."
        )
    visible = actionable[:1] if compact else problems
    if visible:
        text += "\n" + "\n".join("• " + escaped(p["text"], 130) for p in visible)
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
        text += "\n" + switch("Автозавершение", result["complete"])
        if result.get("completion_note"):
            text += "\n" + result["completion_note"]
        text += "\nПолная проверка: " + (
            local_time(result["checked_at"]) if result["checked_at"] else "ещё не выполнялась"
        )
        if result["mode"] == "playerok":
            text += "\nБот проверяет заказ перед автоответом или отметкой выполнения."
        else:
            text += "\nПолучатель и текущий заказ проверяются отдельно перед каждой выдачей."
    return text


def brief(result):
    """Seller summary; detailed evidence stays in the existing diagnostics."""
    mode, state = result["mode"], result["state"]
    if mode == "manual":
        return "📦 Выдача: вручную"
    if mode == "playerok":
        text = "📦 Товар выдаёт Playerok"
        if state == "OFF":
            return text + "\n" + PLATFORM_STATES[state]
    else:
        text = "Выдача: " + MODES[mode]
    labels = {
        "WAITING_PUBLICATION": "⏳ Выдача настроена. Ожидает публикации.",
        "READY": "✅ Автовыдача работает",
        "OFF": "⏸ Автовыдача выключена — отправка остановлена",
        "UNKNOWN": "🔄 Проверяем готовность",
        "BLOCKED": "⚠️ Автовыдача ожидает устранения проблемы",
        "STOPPED": "⛔ Автовыдача приостановлена",
    }
    text += "\n" + (PLATFORM_STATES if mode == "playerok" else labels)[state]
    problems = result["problems"]
    if any(p["code"] == "GIFT_IN_PROGRESS" for p in problems) and state == "UNKNOWN":
        text = "Выдача: " + MODES[mode] + "\n⏳ Отправляем подарок, следующие покупки ожидают"
    actionable = sorted(
        (
            p
            for p in problems
            if p["blocking"]
            and not p["unknown"]
            and not (state == "WAITING_PUBLICATION" and p["code"] == "NOT_PUBLISHED")
        ),
        key=lambda p: not p["terminal"],
    )
    if actionable:
        text += "\n" + escaped(actionable[0]["text"], 170)
        if len(actionable) > 1:
            text += f"\nЕщё причин: {len(actionable) - 1}"
    if mode == "playerok":
        text += "\n💬 " + escaped(result["message"], 150)
    return text


def actions(result):
    rows, seen = [], set()
    for problem in result["problems"]:
        label, route = problem["action"]
        if route not in seen:
            seen.add(route)
            rows.append([(label, route)])
    return rows
