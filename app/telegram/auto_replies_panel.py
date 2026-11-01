import secrets

from app.database.store import Conflict
from app.services.auto_replies import AutoReplies, phrase
from app.services.replies import reply_text
from app.telegram.formatting import escaped, html_pages
from app.telegram.keyboards import keyboard
from app.telegram.presentation import local_time, status


class AutoRepliesPanel:
    def __init__(self, store, api):
        self.store, self.service = store, AutoReplies(store, api)

    def blocked_reason(self):
        account = self.service.api.account
        if not getattr(account, "writes_enabled", False):
            return "Отправка выключена: записи Playerok не разрешены."
        if not getattr(account, "automation_enabled", False):
            return "Отправка выключена: автоматизация не разрешена."
        if getattr(account, "connection_verified", True) is not True:
            return "Отправка остановлена: подтвердите подключение в профиле."
        return ""

    def menu(self, owner, *, listing=False):
        rules = self.service.rules(owner)
        if listing:
            rows = [
                [(("🟢 " if rule["enabled"] else "⚪ ") + rule["trigger_text"][:40], "ar:show:" + rule["id"])]
                for rule in rules
            ]
            rows += [[("➕ Добавить автоответ", "ar:new")]]
            return "💬 <b>Мои автоответы</b>" + ("\nПравил пока нет." if not rules else ""), keyboard(
                rows, back=("⬅️ Автоответы", "ar:menu")
            )
        text = "💬 <b>Автоответы</b>\nБот отвечает покупателям на заданные фразы."
        text += f"\nПравил: {len(rules)} · Включено: {sum(bool(r['enabled']) for r in rules)}"
        reason = self.blocked_reason()
        if reason:
            text += "\n⚠️ " + reason
        return text, keyboard(
            [
                [("➕ Добавить автоответ", "ar:new")],
                [("📋 Мои автоответы", "ar:list")],
                [("⚙️ Настройки", "ar:settings"), ("История ответов", "ar:history:0")],
            ],
            back=("⬅️ Настройки", "settings"),
        )

    def card(self, owner, ident):
        rule = self.service.get(owner, ident)
        if not rule:
            raise Conflict("Правило недоступно")
        text = (
            "💬 <b>Автоответ</b>\nЕсли покупатель напишет:\n«"
            + escaped(rule["trigger_text"], 800)
            + "»\nБот ответит:\n«"
            + escaped(rule["response_text"], 2200)
            + f"»\nСтатус: {'🟢 Включён' if rule['enabled'] else '⚪ Выключен'}"
        )
        if rule["enabled"] and self.blocked_reason():
            text += "\n⚠️ " + self.blocked_reason()
        rows = [
            [("Выключить" if rule["enabled"] else "Включить для будущих сообщений", "ar:toggle:" + ident)],
            [("✏️ Изменить", "ar:edit:" + ident)],
        ]
        if not rule["enabled"]:
            rows.append([("Удалить правило", "ar:delete:" + ident)])
        rows.append([("Лимиты и условия", "ar:details:" + ident)])
        rows.append([("⬅️ Мои автоответы", "ar:list")])
        return text, keyboard(rows)

    async def confirm(self, state, pending, text, message=None):
        response = (pending.get("values") or {}).get("response_text")
        if response:
            if message is None:
                raise Conflict("Откройте превью через панель")
            pages = html_pages(response, 2800)
            for index, page in enumerate(pages, 1):
                await message.answer(f"💬 <b>Полный текст автоответа · {index}/{len(pages)}</b>\n" + page)
        nonce = secrets.token_hex(6)
        await state.set_state("autoreply:confirm")
        await state.set_data({"auto_reply_pending": pending, "auto_reply_nonce": nonce})
        return text, keyboard([[("Подтвердить локально", "ar:commit:" + nonce)], [("Отмена", "ar:menu")]])

    async def callback(self, action, owner, state, message=None):
        self.service.owner(owner)
        if action.startswith("ar:history:"):
            page = int(action.split(":", 2)[2])
            if not 0 <= page <= 1_000_000:
                raise ValueError("Недопустимая страница")
            rows = self.store.rows(
                "SELECT e.*,o.status AS operation_status FROM auto_reply_events e LEFT JOIN outbox o ON o.id=e.operation_id WHERE e.account_id=? ORDER BY e.created_at DESC,e.message_id DESC LIMIT 9 OFFSET ?",
                (self.service.account(), page * 8),
            )
            text = f"💬 <b>Журнал автоответов · {page + 1}</b>\n" + "\n".join(
                local_time(row["created_at"]) + " · " + status(row["operation_status"] or row["status"])
                for row in rows[:8]
            )
            buttons = [
                [("Операция " + str(i + 1), "ops:show:" + row["operation_id"])]
                for i, row in enumerate(rows[:8])
                if row["operation_id"]
            ]
            navigation = []
            if page:
                navigation.append(("← Назад", f"ar:history:{page - 1}"))
            if len(rows) > 8:
                navigation.append(("Далее →", f"ar:history:{page + 1}"))
            if navigation:
                buttons.append(navigation)
            buttons.append([("Правила", "ar:menu")])
            return text + ("\nНет записей." if not rows else ""), keyboard(buttons)
        if action == "ar:list":
            await state.clear()
            return self.menu(owner, listing=True)
        if action == "ar:settings":
            return (
                "⚙️ <b>Настройки автоответов</b>\nТочное совпадение фразы, без учёта регистра и лишних пробелов.\nПауза и суточный лимит задаются в каждом правиле.\nОтправка только на новые сообщения; неопределённый результат не повторяется.",
                keyboard(
                    [
                        [("⛔ Выключить все", "ar:stop")],
                        [("Ручные быстрые ответы", "quickreplies")],
                        [("Подключение и разрешения", "profile")],
                    ],
                    back=("⬅️ Автоответы", "ar:menu"),
                ),
            )
        if action == "ar:menu":
            await state.clear()
            return self.menu(owner)
        if action == "ar:stop":
            count = self.service.stop(owner)
            await state.clear()
            return f"Выключено правил: {count}. Уже переданный серверу запрос остановить нельзя.", keyboard(
                [[("Правила", "ar:menu")]]
            )
        if action == "ar:new" or action.startswith("ar:edit:"):
            rule = self.service.get(owner, action.split(":", 2)[2]) if action.startswith("ar:edit:") else None
            if action.startswith("ar:edit:") and not rule:
                raise Conflict("Правило удалено")
            await state.set_state("autoreply:trigger")
            await state.set_data(
                {
                    "auto_reply_form": {
                        "id": rule["id"] if rule else None,
                        "revision": rule["revision"] if rule else 0,
                        "account_id": self.service.account(),
                    }
                }
            )
            return "Введите точную фразу покупателя (1–200 символов).", keyboard([[("Отмена", "ar:menu")]])
        parts = action.split(":", 2)
        command, ident = parts[1:]
        if command == "commit":
            data = await state.get_data()
            pending = data.get("auto_reply_pending")
            if (
                not pending
                or ident != data.get("auto_reply_nonce")
                or pending["account_id"] != self.service.account()
            ):
                raise Conflict("Подтверждение устарело")
            if pending["kind"] == "delete":
                self.service.delete(owner, pending["id"], pending["revision"])
                result = self.menu(owner)
            else:
                target = self.service.save(
                    owner,
                    pending["values"],
                    ident=pending["id"],
                    revision=pending["revision"],
                    armed=pending.get("armed", False),
                )
                result = self.card(owner, target)
            await state.clear()
            return result
        rule = self.service.get(owner, ident)
        if not rule:
            raise Conflict("Правило недоступно")
        if command == "details":
            return (
                f"⚙️ <b>Условия автоответа</b>\nПауза в чате: {rule['cooldown']} с\nМаксимум попыток в сутки: {rule['daily_limit']}\nЛимит учитывает неуспешные и неопределённые попытки.\nИзменение выключает правило до нового подтверждения.",
                keyboard([[("Изменить", "ar:edit:" + ident)]], back=("⬅️ К автоответу", "ar:show:" + ident)),
            )
        if command == "show":
            await state.clear()
            return self.card(owner, ident)
        pending = {
            "kind": command,
            "id": ident,
            "revision": rule["revision"],
            "account_id": rule["account_id"],
        }
        if command == "toggle":
            pending.update(
                values={**rule, "enabled": not bool(rule["enabled"])}, armed=not bool(rule["enabled"])
            )
            return await self.confirm(
                state,
                pending,
                self.card(owner, ident)[0]
                + (
                    "\n\nРазрешаю автоматическую отправку этого ответа только на новые сообщения после включения. Прежние сообщения не обрабатываются. Общие разрешения бота не изменятся."
                    if pending["armed"]
                    else "\n\nВыключить правило и заблокировать ещё не отправленные автоответы?"
                ),
                message=message,
            )
        if command == "delete":
            return await self.confirm(
                state, pending, "Удалить выключенное правило? Журнал прежних попыток сохраняется."
            )
        raise ValueError("Действие автоответов недоступно")

    async def message(self, message, state):
        owner = message.from_user.id
        self.service.owner(owner)
        data, current = await state.get_data(), await state.get_state()
        form, value = data.get("auto_reply_form"), (message.text or "").strip()
        if not form or form["account_id"] != self.service.account():
            raise Conflict("Форма устарела")
        steps = {
            "autoreply:trigger": (
                "trigger_text",
                "autoreply:response",
                "Введите текст автоответа (1–3500 UTF-16 символов).",
            ),
            "autoreply:response": (
                "response_text",
                "autoreply:cooldown",
                "Введите паузу между ответами в одном чате, 60–86400 секунд (например 600).",
            ),
            "autoreply:cooldown": (
                "cooldown",
                "autoreply:limit",
                "Введите максимум попыток этого правила в сутки, 1–100 (например 20).",
            ),
        }
        if current in steps:
            field, following, prompt = steps[current]
            form[field] = (
                phrase(value)
                if field == "trigger_text"
                else reply_text(value)
                if field == "response_text"
                else int(value)
            )
            if field == "cooldown" and not 60 <= form[field] <= 86400:
                raise ValueError("Пауза: 60–86400 секунд")
            await state.set_data({"auto_reply_form": form})
            await state.set_state(following)
            return prompt, keyboard([[("Отмена", "ar:menu")]])
        if current != "autoreply:limit" or not value.isdigit() or not 1 <= int(value) <= 100:
            raise ValueError("Лимит: целое число 1–100")
        values = {**form, "daily_limit": int(value), "enabled": False}
        return await self.confirm(
            state,
            {
                "kind": "save",
                "id": form["id"],
                "revision": form["revision"],
                "account_id": form["account_id"],
                "values": values,
            },
            "Сохранить выключенное правило?\nФраза: "
            + escaped(form["trigger_text"], 800)
            + "\nОтвет:\n"
            + escaped(form["response_text"], 2200)
            + f"\nПауза: {form['cooldown']} с · лимит: {value}/сутки. Включение требует отдельного подтверждения.",
            message=message,
        )
