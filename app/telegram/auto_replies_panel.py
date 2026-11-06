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
        text = "💬 <b>Ответы на фразы покупателей</b>\nОтдельные правила для переписки. Сообщения после оплаты и выдачи настраиваются в карточке товара."
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
            + ("\nСодержит фразу" if rule["match_mode"] == "contains" else "\nТочное совпадение фразы")
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
        else:
            pages = []
        nonce = secrets.token_hex(6)
        await state.set_state("autoreply:confirm")
        await state.set_data(
            {
                "auto_reply_pending": pending,
                "auto_reply_nonce": nonce,
                "auto_reply_preview": pages,
                "auto_reply_question": text,
            }
        )
        if pages:
            return await self.preview(state, nonce, 0)
        return text, keyboard([[("Подтвердить локально", "ar:commit:" + nonce)], [("Отмена", "ar:menu")]])

    async def preview(self, state, nonce, index):
        data = await state.get_data()
        pages = data.get("auto_reply_preview", [])
        if nonce != data.get("auto_reply_nonce") or not 0 <= index < len(pages):
            raise Conflict("Предпросмотр устарел. Откройте автоответ заново")
        rows = []
        if index:
            rows.append([("⬅️ Предыдущая страница", f"ar:preview:{nonce}:{index - 1}")])
        if index + 1 < len(pages):
            rows.append([("Следующая страница ➡️", f"ar:preview:{nonce}:{index + 1}")])
        else:
            rows.append([("Подтвердить локально", "ar:commit:" + nonce)])
        return f"💬 <b>Полный текст автоответа · {index + 1}/{len(pages)}</b>\n" + pages[
            index
        ] + "\n\n" + data["auto_reply_question"], keyboard(rows, back=("Отмена", "ar:menu"))

    async def callback(self, action, owner, state, message=None):
        self.service.owner(owner)
        if action.startswith("ar:preview:"):
            _, _, nonce, index = action.split(":")
            return await self.preview(state, nonce, int(index))
        if action.startswith("ar:history:"):
            page = int(action.split(":", 2)[2])
            if not 0 <= page <= 1_000_000:
                raise ValueError("Недопустимая страница")
            rows = self.store.rows(
                "SELECT e.*,o.status AS operation_status FROM auto_reply_events e LEFT JOIN outbox o ON o.id=e.operation_id WHERE e.account_id=? ORDER BY e.created_at DESC,e.message_id DESC LIMIT 9 OFFSET ?",
                (self.service.account(), page * 8),
            )
            text = f"💬 <b>Журнал автоответов · {page + 1}</b>\n" + "\n".join(
                local_time(row["created_at"])
                + " · "
                + (
                    "Сообщение получено с задержкой более 5 минут: ответ не отправлялся"
                    if row["error_code"] == "SYNC_DELAY"
                    else status(row["operation_status"] or row["status"])
                )
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
                "⚙️ <b>Настройки автоответов</b>\nСпособ сравнения выбирается в правиле. Регистр и лишние пробелы не учитываются.\nПауза и суточный лимит задаются в каждом правиле.\nОтправка только на новые сообщения; неопределённый результат не повторяется.",
                keyboard(
                    [
                        [("⛔ Выключить все", "ar:stop")],
                        [("Подключение и разрешения", "profile")],
                    ],
                    back=("⬅️ Автоответы", "ar:menu"),
                ),
            )
        if action == "ar:menu":
            await state.clear()
            return self.menu(owner)
        if action == "ar:stop" or action.startswith("ar:stop_confirm:"):
            from app.telegram.stop_confirmation import StopConfirmation

            confirmation = StopConfirmation(self.store, self.service.api)
            if action == "ar:stop":
                return await confirmation.request("ar", owner, state)
            count = await confirmation.confirm("ar", owner, state, action.split(":", 2)[2], self.service.stop)
            return f"Выключено правил: {count}. Уже переданный серверу запрос остановить нельзя.", keyboard(
                [[("Правила", "ar:menu")]]
            )
        if action.startswith("ar:match:"):
            _, _, ident, mode = action.split(":")
            data = (await state.get_data()).get("auto_reply_edit") or {}
            rule = self.service.get(owner, ident)
            if (
                not rule
                or data.get("id") != ident
                or data.get("revision") != rule["revision"]
                or data.get("account_id") != self.service.account()
            ):
                raise Conflict("Правило изменилось. Откройте его заново")
            self.service.save(
                owner,
                {**rule, "enabled": bool(rule["enabled"]), "match_mode": mode},
                ident=ident,
                revision=rule["revision"],
                armed=bool(rule["enabled"]),
            )
            await state.clear()
            return self.card(owner, ident)
        if action.startswith("ar:field:"):
            _, _, ident, field = action.split(":")
            rule = self.service.get(owner, ident)
            if not rule or field not in {"trigger_text", "response_text", "cooldown", "daily_limit"}:
                raise Conflict("Поле правила недоступно")
            await state.set_state("autoreply:field")
            await state.set_data(
                {"auto_reply_form": {**rule, "enabled": bool(rule["enabled"]), "field": field}}
            )
            prompt = {
                "trigger_text": "Введите фразу покупателя (1–200 символов).",
                "response_text": "Введите новый текст ответа (1–3500 UTF-16 символов).",
                "cooldown": "Введите паузу в секундах: 60–86400.",
                "daily_limit": "Введите максимум попыток в сутки: 1–100.",
            }[field]
            return prompt, keyboard([], back=("Отмена", "ar:show:" + ident))
        if action.startswith("ar:edit:"):
            ident = action.split(":", 2)[2]
            rule = self.service.get(owner, ident)
            if not rule:
                raise Conflict("Правило удалено")
            await state.update_data(
                auto_reply_edit={
                    "id": ident,
                    "revision": rule["revision"],
                    "account_id": self.service.account(),
                }
            )
            return "✏️ Что изменить в автоответе? Остальные настройки сохранятся.", keyboard(
                [
                    [("Фраза покупателя", "ar:field:" + ident + ":trigger_text")],
                    [("Текст ответа", "ar:field:" + ident + ":response_text")],
                    [
                        ("Пауза", "ar:field:" + ident + ":cooldown"),
                        ("Лимит в сутки", "ar:field:" + ident + ":daily_limit"),
                    ],
                    [
                        ("Точное совпадение", "ar:match:" + ident + ":exact"),
                        ("Содержит фразу", "ar:match:" + ident + ":contains"),
                    ],
                ],
                back=("К автоответу", "ar:show:" + ident),
            )
        if action == "ar:new":
            await state.set_state("autoreply:trigger")
            await state.set_data(
                {
                    "auto_reply_form": {
                        "id": None,
                        "revision": 0,
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
                f"⚙️ <b>Условия автоответа</b>\nПауза в чате: {rule['cooldown']} с\nМаксимум попыток в сутки: {rule['daily_limit']}\nЛимит учитывает неуспешные и неопределённые попытки.\nИзменение одного поля сохраняет остальные настройки и состояние правила.",
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
        if current == "autoreply:field":
            field = form["field"]
            values = {
                **form,
                field: phrase(value)
                if field == "trigger_text"
                else reply_text(value)
                if field == "response_text"
                else int(value),
            }
            ident = self.service.save(
                owner, values, ident=form["id"], revision=form["revision"], armed=form["enabled"]
            )
            await state.clear()
            return self.card(owner, ident)
        if current == "autoreply:response":
            ident = self.service.save(owner, {**form, "response_text": reply_text(value), "enabled": False})
            await state.clear()
            return self.card(owner, ident)
        if current == "autoreply:trigger":
            form["trigger_text"] = phrase(value)
            await state.set_data({"auto_reply_form": form})
            await state.set_state("autoreply:response")
            return "Введите текст автоответа (1–3500 UTF-16 символов).", keyboard(
                [], back=("Отмена", "ar:menu")
            )
        raise Conflict("Старая форма создания устарела. Откройте автоответ заново")
