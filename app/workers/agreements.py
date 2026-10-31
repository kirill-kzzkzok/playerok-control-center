"""Resume only the owner's still-open agreement-waiting wizard, with FSM isolation."""

import asyncio
import json

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict
from app.playerok.adapter import APIError
from app.services.products_errors import ValidationError
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard


class AgreementResumer:
    def __init__(self, store, api, products, dispatcher, bot):
        self.store, self.api, self.products = store, api, products
        self.dispatcher, self.bot = dispatcher, bot

    async def once(self):
        if not getattr(self.api.account, "auto_accept_agreements", False):
            return
        storage = SQLiteStorage(self.store)
        for row in self.store.rows("SELECT key,data FROM fsm WHERE state='wizard'"):
            parts = json.loads(row["key"])
            if len(parts) != 6 or parts[0] != self.bot.id:
                continue
            if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (parts[2],)):
                continue
            key = StorageKey(
                bot_id=parts[0],
                chat_id=parts[1],
                user_id=parts[2],
                thread_id=parts[3],
                business_connection_id=parts[4],
                destiny=parts[5],
            )
            async with self.dispatcher.fsm.events_isolation.lock(key=key):
                state = FSMContext(storage, key)
                data = await state.get_data()
                if await state.get_state() != "wizard" or not data.get("draft_id"):
                    continue
                try:
                    draft = self.store.get_draft(data["draft_id"], key.user_id)
                    if draft["state"] != "DRAFT" or draft["payload"].get("step") != "agreements_wait":
                        continue
                    wizard = Wizard(self.store, self.api, self.products)
                    await wizard.load_form(draft["payload"], draft["payload"].get("obtaining_type_id"))
                    if draft["payload"]["step"] == "agreements_wait":
                        continue
                    await state.update_data(revision=draft["revision"])
                    await wizard.save(key.user_id, state, draft["payload"])
                    text, markup = await wizard.screen(key.user_id, state)
                    # State is already durable. A failed/unknown owner notice is
                    # not resent and cannot repeat acceptance; reopen the draft.
                    await self.bot.send_message(
                        key.chat_id, "✅ Условия Playerok подтверждены\n" + text, reply_markup=markup
                    )
                except (APIError, ValidationError, Conflict, TelegramAPIError):
                    continue

    async def run(self, stop):
        while not stop.is_set():
            await self.once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                continue
