"""Owner's gift control in the existing Dispatcher; SQLite FSM confirmations survive restart."""

import asyncio
import json
import secrets
from collections import Counter
from html import escape

from app.database.store import Conflict
from app.gifts.core import SEED_GIFTS, GiftValidationError, gift_code_from_description, gift_reason
from app.gifts.mtproto import GiftFailure, PersonalGiftSender
from app.gifts.service import GiftService
from app.gifts.settings import GiftSettings
from app.telegram.keyboards import keyboard


class GiftsPanel:
    def __init__(self, store, api):
        settings = getattr(api, "gift_settings", None) or GiftSettings.load()
        if not getattr(api, "gift_sender", None):
            api.gift_sender = PersonalGiftSender(settings)
        self.service = GiftService(store, api, settings, api.gift_sender)
        self.store = store

    def summary(self, item_id):
        return self.service.status_text(item_id)

    @staticmethod
    def bundle_text(bundle):
        counts = Counter(g["code"] for g in bundle)
        prices = {g["code"]: g["stars"] for g in bundle}
        lines = ["🎁 Привязка подарков"]
        for code, count in counts.items():
            lines.append(f"{SEED_GIFTS[code][0]} {escape(code)} ×{count} — {prices[code] * count} Stars")
        lines += [f"Всего: {len(bundle)} подарка", f"Стоимость: {sum(g['stars'] for g in bundle)} Stars"]
        return "\n".join(lines)

    async def callback(self, action, owner, state):
        try:
            return await self._callback(action, owner, state)
        except (GiftValidationError, GiftFailure, Conflict):
            raise
        except Exception:  # noqa: BLE001 -- panel boundary must never expose raw MTProto exceptions
            raise GiftValidationError(
                "Не удалось проверить Telegram/Playerok. Расходы не выполнялись; проверьте подключение."
            ) from None

    async def _callback(self, action, owner, state):
        s = self.service
        s.owner(owner)
        parts = action.split(":")
        command = parts[1]
        if command == "home":
            await state.clear()
            v = self.store.setting("gifts:connection", {})
            b = self.store.setting("gifts:balance", {})
            order, day = s.limits()
            return (
                (
                    "🎁 <b>Telegram-подарки с личного аккаунта</b>\n"
                    f"Подключение (последняя проверка): {escape(v.get('status', 'не проверено'))}\n"
                    f"Баланс: {b.get('stars', 'не проверен')} ⭐ · {escape(b.get('at', 'нет даты'))}\n"
                    f"Защитные лимиты: {order} ⭐ / заказ, {day} ⭐ / сутки UTC\n"
                    f"Платёжный допуск .env: {'разрешён' if s.settings.payment_enabled else 'выключен'}\n"
                    f"Общая остановка: {'да' if self.store.setting('gifts:stopped', True) else 'нет'}\n"
                    "Выдача только после PAID, из точного поля получения. Каждый лот включается отдельно.\n"
                    f"Автовозвраты: {'разрешены; только допустимый PAID и окончательный отказ username' if s.settings.refund_enabled else 'выключены; ручное решение'}.\n"
                    "Авторизация аккаунта: локально python -m app.gifts.auth."
                ),
                keyboard(
                    [
                        [("🔄 Подключение / баланс / каталог", "gft:refresh")],
                        [("📚 Каталог", "gft:catalog"), ("📦 Привязанные лоты", "gft:bindings")],
                        [("➕ Выбрать лот", "items"), ("💰 Лимиты", "gft:limits")],
                        [("📋 Проверить описание", "gft:description")],
                        [("✅ Успешные", "gft:list:success:0"), ("⚠️ Ошибки", "gft:list:failed:0")],
                        [("↩️ Ожидают возврата", "gft:list:refund:0"), ("Возвращены", "gft:list:refunded:0")],
                        [("❓ UNCERTAIN", "gft:list:uncertain:0")],
                        [("⛔ Экстренно остановить покупки", "gft:stop")],
                    ]
                ),
            )
        if command == "refresh":
            try:
                async with asyncio.timeout(30):
                    await s.refresh()
            except Exception:  # noqa: BLE001 -- never reflect account/session errors
                self.store.set_setting("gifts:connection", {"status": "CHECK_FAILED"})
                raise GiftValidationError(
                    "Личный Telegram недоступен. Проверьте локальную авторизацию; расходы не выполнялись."
                ) from None
            return await self.callback("gft:home", owner, state)
        if command == "catalog":
            entries = {v["code"]: v for v in self.store.rows("SELECT * FROM gift_catalog")}
            lines = []
            for code, (emoji, _, seed) in SEED_GIFTS.items():
                v = entries.get(code)
                lines.append(
                    f"{emoji} <code>{code}</code>: "
                    + (
                        f"{v['stars']} ⭐ · {'доступен' if v['available'] else 'недоступен'} · {escape(v['checked_at'])}"
                        if v
                        else f"снимок {seed} ⭐; актуальная цена не проверена"
                    )
                )
            return "🎁 Каталог\n" + "\n".join(lines), keyboard(
                [[("🔄 Проверить Telegram", "gft:refresh"), ("Назад", "gft:home")]]
            )
        if command == "bindings":
            rows = self.store.rows("SELECT * FROM gift_listings ORDER BY item_id LIMIT 40")
            return "🎁 Привязанные лоты (до 40)", keyboard(
                [
                    [
                        (
                            r["gift_code"] + " · " + ("включён" if r["enabled"] else "остановлен"),
                            "gft:item:" + r["item_id"],
                        )
                    ]
                    for r in rows
                ]
                + [[("Назад", "gft:home")]]
            )
        if command == "stop":
            s.stop(owner)
            await state.clear()
            return (
                "Новые покупки остановлены. Уже отправленный запрос отменить невозможно. Проверьте UNCERTAIN. Лоты потребуется включить заново.",
                keyboard([[("Операции UNCERTAIN", "gft:list:uncertain:0"), ("Назад", "gft:home")]]),
            )
        if command in {"limits", "description"}:
            await state.clear()
            await state.set_state("gifts:" + command)
            return (
                "Введите лимиты Stars: за заказ и за сутки через пробел. Повышение выше защитных лимитов .env запрещено."
                if command == "limits"
                else "Пришлите описание: проверю единственную последнюю строку gift: code. Само описание не будет повторено в ответе."
            ), keyboard([[("Отмена", "gft:home")]])
        if command == "save_limits":
            data = (await state.get_data()).get("gift_limits")
            if not data or data["nonce"] != parts[2]:
                raise Conflict("Подтверждение устарело")
            s.set_limits(owner, data["order"], data["day"])
            await state.clear()
            return await self.callback("gft:home", owner, state)
        if command == "list":
            group, page = parts[2], int(parts[3])
            predicates = {
                "success": "state='SUCCEEDED'",
                "failed": "state IN ('BLOCKED','INVALID_RECIPIENT')",
                "refund": "refund_state IN ('MANUAL','QUEUED','SPENDING','FAILED','UNCERTAIN')",
                "refunded": "refund_state='CONFIRMED'",
                "uncertain": "state='UNCERTAIN' OR refund_state='UNCERTAIN'",
            }
            if group not in predicates or not 0 <= page <= 100000:
                raise Conflict("Некорректная страница")
            jobs = self.store.rows(
                f"SELECT order_id,state,refund_state FROM gift_jobs WHERE {predicates[group]} ORDER BY created_at DESC LIMIT 11 OFFSET ?",
                (page * 10,),
            )
            rows = [[(j["order_id"] + " · " + j["state"], "gft:job:" + j["order_id"])] for j in jobs[:10]]
            if len(jobs) > 10:
                rows.append([("Далее", f"gft:list:{group}:{page + 1}")])
            if page:
                rows.append([("Назад", f"gft:list:{group}:{page - 1}")])
            return "🎁 Заказы: " + group + ("\nПока пусто" if not jobs else ""), keyboard(
                rows + [[("К подаркам", "gft:home")]]
            )
        if command in {"job", "reconcile", "refundcheck"}:
            ident = parts[2]
            if command != "job":
                try:
                    async with asyncio.timeout(30):
                        if command == "reconcile":
                            await s.reconcile(owner, ident)
                        else:
                            await s.reconcile_refund(owner, ident)
                except Exception:  # noqa: BLE001 -- read failure cannot authorize a paid retry
                    raise GiftValidationError(
                        "Достоверного подтверждения пока нет. Повторной отправки/возврата не было; требуется ручная проверка."
                    ) from None
            j = s.job(ident)
            text = (
                f"🎁 Заказ <code>{escape(ident)}</code>\nПодарок: {escape(j['gift_code'])}\n"
                f"Выдача: {j['state']}\nВозврат: {j['refund_state']}\n"
                f"Расход (intent): {j['stars'] if j['stars'] is not None else 'не начат'} ⭐\n"
                f"Причина: {escape(j['error_code'] or 'нет')}\n"
                "Получатель скрыт в общем журнале. Нет автоматического повтора UNCERTAIN."
            )
            steps = s.steps(ident)
            done = sum(g["state"] == "SUCCEEDED" for g in steps)
            text += f"\nПрогресс: {done}/{len(steps)}"
            confirmed = sum(g["stars"] for g in steps if g["state"] == "SUCCEEDED")
            unknown = sum(g["stars"] for g in steps if g["state"] in {"SPENDING", "UNCERTAIN"})
            text += f"\nПодтверждено: {confirmed} Stars · неизвестный расход: {unknown} Stars"
            text += f"\nСогласованная стоимость пакета: {sum(g['stars'] for g in steps)} Stars"
            if done and done < len(steps):
                text += "\n⚠️ PARTIALLY_DELIVERED — полный возврат и SENT запрещены"
            for g in steps:
                icon = (
                    "✅"
                    if g["state"] == "SUCCEEDED"
                    else "❓"
                    if g["state"] == "UNCERTAIN"
                    else "⛔"
                    if g["state"] == "BLOCKED"
                    else "⏳"
                )
                text += f"\n{icon} {escape(g['code'])} {g['position'] + 1}/{len(steps)} · {g['stars']} ⭐ · {g['state']}"
                if g["error_code"]:
                    text += (
                        "\nПричина: " + escape(gift_reason(g["error_code"])) + " · " + escape(g["error_code"])
                    )
                if g["message_id"]:
                    text += f"\nСерверное подтверждение: сообщение {g['message_id']}"
                if g["spent_at"]:
                    text += "\nПлатёжное намерение: " + escape(g["spent_at"])
            rows = [[("Открыть заказ", "order:" + ident)]]
            for g in steps:
                if g["operation_id"]:
                    rows.append(
                        [
                            (
                                f"История {g['code']} {g['position'] + 1}/{len(steps)}",
                                "ops:show:" + g["operation_id"],
                            )
                        ]
                    )
            if j["state"] == "BLOCKED" and any(
                g["state"] == "BLOCKED" and not g["spent_at"] and not g["form_id"] for g in steps
            ):
                rows.append([("Повторить только безопасную проверку", "gft:retrycheck:" + ident)])
            if j["state"] == "SUCCEEDED":
                text += "\nВыдано — проверьте завершение сделки. Для отчёта с полями требуется существующая форма Fulfillment."
            if j["state"] == "UNCERTAIN" and j["spent_at"]:
                rows.append([("🔎 Сверить доставку (только чтение)", "gft:reconcile:" + ident)])
            if j["refund_state"] in {"MANUAL", "UNCERTAIN", "FAILED"} and not j["spent_at"]:
                rows.append([("🔎 Проверить возврат на сервере", "gft:refundcheck:" + ident)])
            rows.append([("К подаркам", "gft:home")])
            return text, keyboard(rows)
        if command == "confirmbind":
            p = (await state.get_data()).get("gift_bind")
            if not p or p["nonce"] != parts[2]:
                raise Conflict("Подтверждение устарело")
            data = await s.inspect_item(owner, p["item"])
            if data["hash"] != p["hash"]:
                raise Conflict("Описание изменилось; выберите код заново")
            await s.bind(
                owner,
                p["item"],
                p["code"],
                expected_stars=p["stars"],
                expected_revision=p["revision"],
                expected_bundle=p.get("bundle"),
            )
            await state.clear()
            return (
                "Привязка и актуальная цена сохранены. Платные отправки остаются выключенными для лота.",
                keyboard([[("К привязке", "gft:item:" + p["item"])]]),
            )
        if command in {"retrycheck", "confirmretry"}:
            ident = parts[2]
            j = s.job(ident)
            if command == "retrycheck":
                nonce = secrets.token_hex(5)
                await state.clear()
                await state.set_data({"gift_retry": {"order": ident, "nonce": nonce}})
                return (
                    "Продолжить с первой неотправленной позиции? Её предыдущая проверка остановлена до платёжного намерения. Доставленные подарки не повторяются. Возможна реальная покупка оставшихся подарков. UNCERTAIN не повторяется.",
                    keyboard(
                        [
                            [("Подтверждаю", f"gft:confirmretry:{ident}:{nonce}")],
                            [("Отмена", "gft:job:" + ident)],
                        ]
                    ),
                )
            pending = (await state.get_data()).get("gift_retry")
            if len(parts) != 4 or pending != {"order": ident, "nonce": parts[3]}:
                raise Conflict("Подтверждение устарело")
            s.retry_preflight(owner, ident)
            await state.clear()
            return "Безопасная проверка поставлена в очередь. Отправка пока не выполнена.", keyboard(
                [[("Заказ", "gft:job:" + ident)]]
            )
        ident = parts[2]
        if command == "item":
            data = await s.inspect_item(owner, ident)
            l = s.listing(ident)
            text = self.summary(ident) + f"\nМетка описания корректна: gift: {escape(data['code'])}\n"
            text += (
                "Поле на Playerok обязательное."
                if data["required"]
                else "⚠️ На Playerok поле необязательное. Приложение запрещает отправку без его значения."
            )
            if l:
                text += "\n" + self.bundle_text(json.loads(l["bundle"]))
            text += "\nСписок и актуальная цена подтверждаются при привязке."
            rows = [[("Подтвердить код / обновить привязку", "gft:bind:" + ident)]]
            if l:
                rows += (
                    [[("Остановить лот", "gft:off:" + ident)]]
                    if l["enabled"]
                    else [[("Включить для будущих заказов", "gft:enable:" + ident)]]
                )
            return text, keyboard(rows + [[("К лоту", "item:" + ident), ("К подаркам", "gft:home")]])
        if command == "bind":
            data = await s.inspect_item(owner, ident)
            await s.refresh()
            bundle = s.catalog_bundle(data["code"])
            total = sum(g["stars"] for g in bundle)
            nonce = secrets.token_hex(5)
            await state.clear()
            await state.set_data(
                {
                    "gift_bind": {
                        "item": ident,
                        "code": data["code"],
                        "hash": data["hash"],
                        "nonce": nonce,
                        "stars": total,
                        "bundle": bundle,
                        "revision": (s.listing(ident) or {}).get("revision", 0),
                    }
                }
            )
            return (
                self.bundle_text(bundle) + "\nАвтовыдача останется выключена.",
                keyboard(
                    [
                        [("Подтвердить пакет", "gft:confirmbind:" + nonce)],
                        [("Отмена", "gft:item:" + ident)],
                    ]
                ),
            )
        if command == "off":
            s.disarm(owner, ident)
            return "Новые покупки лота остановлены. Выполненный запрос отменить нельзя.", keyboard(
                [[("Назад", "gft:item:" + ident)]]
            )
        if command == "enable":
            l = s.listing(ident)
            if not l:
                raise GiftValidationError("Сначала подтвердите код подарка")
            nonce = secrets.token_hex(5)
            await state.clear()
            await state.set_data({"gift_arm": {"item": ident, "revision": l["revision"], "nonce": nonce}})
            return (
                (
                    f"⚠️ Разрешить реальные платные отправки {escape(l['gift_code'])} за {l['approved_stars']} ⭐ по новым PAID заказам?\n"
                    "Лимиты и проверки получателя обязательны. UNCERTAIN блокирует новые покупки.\n"
                    "При отдельном допуске автовозврата окончательный отказ username отменит допустимую PAID сделку; иначе — ручное решение.\n"
                    "Автоматическое SENT доступно только после подтверждённой доставки и для legacy Fulfillment; отчёт с полями остаётся ручным."
                ),
                keyboard(
                    [
                        [("Разрешаю подарки; завершать вручную", f"gft:arm:{ident}:{nonce}:0")],
                        [("Разрешаю подарки + допустимый авто-SENT", f"gft:arm:{ident}:{nonce}:1")],
                        [("Отмена", "gft:item:" + ident)],
                    ]
                ),
            )
        if command == "arm":
            p = (await state.get_data()).get("gift_arm")
            if (
                len(parts) != 5
                or not p
                or p["item"] != ident
                or p["nonce"] != parts[3]
                or parts[4] not in {"0", "1"}
            ):
                raise Conflict("Подтверждение устарело")
            await s.arm(owner, ident, p["revision"], auto_fulfill=parts[4] == "1")
            await state.clear()
            return "Подарки включены только для будущих оплаченных заказов этого лота.", keyboard(
                [[("К лоту", "gft:item:" + ident)]]
            )
        raise GiftValidationError("Неизвестная кнопка подарков")

    async def message(self, message, state):
        self.service.owner(message.from_user.id)
        current = await state.get_state()
        text = message.text or ""
        if current == "gifts:description":
            code = gift_code_from_description(text, max_count=self.service.settings.max_gifts)
            await state.clear()
            return (
                f"Метка корректна: <code>gift: {escape(code)}</code>. Привязка лота и отправки не изменены.",
                keyboard([[("К подаркам", "gft:home")]]),
            )
        values = text.split()
        if len(values) != 2 or any(not v.isascii() or not v.isdecimal() or len(v) > 8 for v in values):
            raise GiftValidationError("Введите два целых положительных числа через пробел")
        nonce = secrets.token_hex(5)
        await state.set_state(None)
        await state.set_data(
            {"gift_limits": {"order": int(values[0]), "day": int(values[1]), "nonce": nonce}}
        )
        return f"Сохранить лимиты {int(values[0])} ⭐ за заказ / {int(values[1])} ⭐ за сутки UTC?", keyboard(
            [[("Сохранить", "gft:save_limits:" + nonce)], [("Отмена", "gft:home")]]
        )
