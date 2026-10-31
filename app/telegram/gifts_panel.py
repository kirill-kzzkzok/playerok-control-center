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
from app.telegram.presentation import item_label, order_label, status


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
            enabled = self.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1 LIMIT 1")
            stopped = self.store.setting("gifts:stopped", True)
            active = bool(
                s.settings.payment_enabled
                and not stopped
                and enabled
                and getattr(s.api.account, "writes_enabled", False)
                and getattr(s.api.account, "automation_enabled", False)
            )
            rows = [
                [("📦 Товары с подарками", "gft:bindings")],
                [("🛒 История выдачи", "gft:history")],
                [("⚙️ Настроить", "gft:settings")],
            ]
            if not stopped or enabled:
                rows.insert(0, [("⛔ Остановить выдачу", "gft:stop")])
            return (
                "🎁 <b>Telegram-подарки</b>\n"
                + "Аккаунт: "
                + ("проверен ранее" if v.get("status") == "OK" else "нужна проверка")
                + "\nБаланс по последней проверке: "
                + str(b.get("stars", "неизвестен"))
                + " ⭐"
                + "\nПроверено: "
                + escape(b.get("at") or "ещё не проверялось")
                + "\nАвтовыдача: "
                + ("разрешена для включённых товаров" if active else "выключена"),
                keyboard(rows, back=("⬅️ Мой магазин", "home")),
            )
        if command == "history":
            await state.clear()
            return await self._callback("gft:list:all:0", owner, state)
        if command == "settings":
            await state.clear()
            order, day = s.limits()
            return (
                "⚙️ <b>Настройки подарков</b>\nЛимит заказа: "
                + str(order)
                + " ⭐\nЛимит суток: "
                + str(day)
                + " ⭐"
                + "\nПлатёжный допуск: "
                + ("включён" if s.settings.payment_enabled else "выключен")
                + "\nАвтовозврат: "
                + ("разрешён с проверками" if s.settings.refund_enabled else "выключен"),
                keyboard(
                    [
                        [("🔄 Проверить подключение и баланс", "gft:refresh")],
                        [("Каталог подарков", "gft:catalog")],
                        [("Лимиты расходов", "gft:limits")],
                        [("Проверить метку в описании", "gft:description")],
                        [("⛔ Остановить выдачу", "gft:stop")],
                    ],
                    back=("⬅️ Подарки", "gft:home"),
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
            page = int(parts[2]) if len(parts) == 3 else 0
            if not 0 <= page <= 100000:
                raise Conflict("Страница недоступна")
            rows = self.store.rows(
                "SELECT * FROM gift_listings ORDER BY item_id LIMIT 11 OFFSET ?", (page * 10,)
            )
            buttons = [
                [
                    (
                        (
                            item_label(self.store, r["item_id"])[:30]
                            + " · "
                            + str(len(json.loads(r["bundle"])))
                            + " 🎁 · "
                            + str(r["approved_stars"])
                            + " ⭐"
                        )[:64],
                        "gft:item:" + r["item_id"],
                    )
                ]
                for r in rows[:10]
            ]
            nav = []
            if page:
                nav.append(("← Назад", f"gft:bindings:{page - 1}"))
            if len(rows) > 10:
                nav.append(("Далее →", f"gft:bindings:{page + 1}"))
            if nav:
                buttons.append(nav)
            buttons.append([("➕ Выбрать товар", "items")])
            return "🎁 <b>Товары с подарками</b>\n" + (
                "Выберите товар для настройки." if rows else "Привязок пока нет. Выберите подходящий товар."
            ), keyboard(buttons, back=("⬅️ Подарки", "gft:home"))
        if command == "stop":
            s.stop(owner)
            await state.clear()
            return (
                "Новые покупки остановлены. Уже отправленный запрос отменить невозможно. Проверьте UNCERTAIN. Лоты потребуется включить заново.",
                keyboard([[("Требуют проверки", "gft:list:uncertain:0"), ("Назад", "gft:home")]]),
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
                "all": "1=1",
                "success": "state='SUCCEEDED'",
                "progress": "state NOT IN ('SUCCEEDED','BLOCKED','INVALID_RECIPIENT','UNCERTAIN')",
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
            rows = [
                [
                    (
                        (order_label(self.store, j["order_id"])[:30] + " · " + status(j["state"]))[:64],
                        "gft:job:" + j["order_id"],
                    )
                ]
                for j in jobs[:10]
            ]
            if len(jobs) > 10:
                rows.append([("Далее", f"gft:list:{group}:{page + 1}")])
            if page:
                rows.append([("Назад", f"gft:list:{group}:{page - 1}")])
            if group == "all":
                rows += [
                    [("Доставлено", "gft:list:success:0"), ("В процессе", "gft:list:progress:0")],
                    [
                        ("Требует проверки", "gft:list:uncertain:0"),
                        ("Остановлено / частично", "gft:list:failed:0"),
                    ],
                    [
                        ("Ожидают возврата", "gft:list:refund:0"),
                        ("Возврат подтверждён", "gft:list:refunded:0"),
                    ],
                ]
            return "🎁 " + {
                "all": "История выдачи",
                "success": "Доставлено",
                "progress": "В процессе",
                "failed": "Заблокировано / частичная выдача",
                "refund": "Ожидают возврата",
                "refunded": "Возврат подтверждён",
                "uncertain": "Требует проверки",
            }[group] + ("\nПока пусто" if not jobs else ""), keyboard(rows + [[("К подаркам", "gft:home")]])
        if command in {"job", "details", "reconcile", "refundcheck"}:
            ident = parts[2]
            if command not in {"job", "details"}:
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
                f"Выдача: {status(j['state'])}\nВозврат: {j['refund_state']}\n"
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
            if command == "job":
                text = (
                    "📦 <b>Подарочный заказ</b>\n"
                    + status(j["state"])
                    + f"\nПрогресс: {done}/{len(steps)} · подтверждено {confirmed} ⭐"
                )
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
                    text += f"\n{icon} {escape(g['code'])} {g['position'] + 1}/{len(steps)} · {status(g['state'])}"
                if done and done < len(steps):
                    text += "\n⚠️ Частичная выдача. Полный возврат и отметка выполнения запрещены."
                if j["error_code"]:
                    text += "\n" + escape(gift_reason(j["error_code"]))
                rows = [
                    [("Открыть заказ", "order:" + ident)],
                    [("⋯ История и решение проблемы", "gft:details:" + ident)],
                ]
            return text, keyboard(rows, back=("⬅️ История подарков", "gft:history"))

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
