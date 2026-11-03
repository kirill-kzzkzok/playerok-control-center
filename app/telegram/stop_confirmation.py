"""Versioned owner confirmation for existing local mass-stop services."""

import hashlib
import json
import secrets
import time

from app.database.store import Conflict, encode
from app.telegram.keyboards import keyboard

LABELS = {
    "gft": ("Остановить подарочные покупки", "gft:home"),
    "auto": ("Остановить автовыдачу склада, текста и действия Playerok", "automation"),
    "promo": ("Остановить автопродвижение", "promotions"),
    "ar": ("Выключить все ответы на фразы покупателей", "ar:menu"),
}


class StopConfirmation:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def snapshot(self, kind, owner):
        if kind not in LABELS or not self.store.rows(
            "SELECT 1 FROM administrators WHERE user_id=?", (owner,)
        ):
            raise Conflict("Доступ закрыт")
        data = {"account": self.api.account.id, "owner": owner, "kind": kind}
        if kind in {"gft", "auto"}:
            gift = kind == "gft"
            products = self.store.rows(
                "SELECT * FROM logical_products WHERE owner_id=? AND "
                + (
                    "handler='telegram_gifts'"
                    if gift
                    else "(handler IN ('unique_stock','fixed_text') OR json_extract(config,'$.platform_only')=1)"
                )
                + " ORDER BY id",
                (owner,),
            )
            table = "gift_listings" if gift else "delivery_policies"
            projections = self.store.rows(f"SELECT * FROM {table} ORDER BY item_id")
            links = self.store.rows("SELECT item_id,product_id FROM logical_listings ORDER BY item_id")
            linked = {row["item_id"] for row in links}
            count = sum(bool(p["enabled"]) for p in products) + sum(
                bool(row.get("enabled") if gift else json.loads(row["payload"]).get("enabled"))
                for row in projections
                if row["item_id"] not in linked
            )
            data.update(products=products, projections=projections, links=links)
            if gift:
                data["stop_revision"] = self.store.setting("gifts:stop_revision", 0)
                data["stopped"] = self.store.setting("gifts:stopped", True)
                data["paused"] = self.store.setting("gifts:paused", False)
        elif kind == "promo":
            data["policy"] = self.store.rows("SELECT * FROM promotion_policy ORDER BY id")
            data["targets"] = self.store.rows("SELECT * FROM promotion_targets ORDER BY item_id")
            data["permission"] = self.store.setting("promotions:paid_authority", {})
            count = len(data["targets"])
        else:
            data["rules"] = self.store.rows(
                "SELECT * FROM auto_reply_rules WHERE owner_id=? AND account_id=? ORDER BY id",
                (owner, self.api.account.id),
            )
            count = sum(bool(row["enabled"]) for row in data["rules"])
        return hashlib.sha256(encode(data).encode()).hexdigest(), count

    async def request(self, kind, owner, state):
        snapshot, count = self.snapshot(kind, owner)
        nonce = secrets.token_hex(6)
        await state.clear()
        await state.set_data(
            {
                "mass_stop": {
                    "owner": owner,
                    "kind": kind,
                    "nonce": nonce,
                    "snapshot": snapshot,
                    "at": time.time(),
                }
            }
        )
        label, back = LABELS[kind]
        scope = f"Затронуто товаров или правил: {count}."
        if kind == "promo":
            scope = f"Явно выбранных товаров: {count}. Также остановятся остальные товары общего расписания, если оно включено."
        return (
            "⛔ <b>"
            + label
            + "?</b>\n"
            + scope
            + "\nНовые автоматические действия остановятся. Уже начатый внешний запрос отменить нельзя; его результат сохраняется и требует сверки при неопределённости. История не удаляется. Автоматического возобновления не будет.",
            keyboard(
                [[("⛔ Подтвердить остановку", kind + ":stop_confirm:" + nonce)]], back=("Отмена", back)
            ),
        )

    async def confirm(self, kind, owner, state, nonce, stop):
        pending = (await state.get_data()).get("mass_stop", {})
        if (pending.get("kind"), pending.get("owner"), pending.get("nonce")) != (
            kind,
            owner,
            nonce,
        ) or not 0 <= time.time() - pending.get("at", 0) <= 300:
            raise Conflict("Подтверждение остановки устарело. Откройте текущие настройки")
        target = kind + ":" + nonce
        with self.store.transaction():
            if self.snapshot(kind, owner)[0] != pending["snapshot"] or self.store.rows(
                "SELECT 1 FROM audit_log WHERE actor=? AND action='CONFIRM_GLOBAL_STOP' AND target=?",
                (owner, target),
            ):
                raise Conflict("Настройки уже изменились. Проверьте текущий список перед остановкой")
            result = stop(owner)
            self.store.audit(owner, "CONFIRM_GLOBAL_STOP", target, "CONSUMED")
        await state.clear()
        return result
