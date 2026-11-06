"""One durable Telegram presentation per existing Playerok conversation.

History remains chat_messages; outbound replies remain the existing Outbox.
"""

import asyncio
import hashlib
import json
import secrets
import time

from app.database.store import Conflict, encode, now
from app.services.replies import Replies
from app.telegram.errors import REJECTED_CONTENT, send_failure, telegram_error
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.panel_tracking import panel_owner
from app.telegram.presentation import local_time


class ChatCards:
    def __init__(self, store, bot):
        self.store, self.bot = store, bot
        if not hasattr(store, "chat_card_lock"):
            store.chat_card_lock = asyncio.Lock()

    @property
    def account(self):
        return self.store.setting("playerok:account_id", "")

    def touch(self, owner, chat_id):
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (owner,)).fetchone():
                raise Conflict("Доступ закрыт")
            db.execute(
                "INSERT INTO chat_cards(account_id,owner_id,chat_id,token,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(account_id,owner_id,chat_id) DO UPDATE SET state=CASE WHEN state='CLOSED' THEN 'NEW' ELSE state END,token=CASE WHEN state='CLOSED' THEN excluded.token ELSE token END,telegram_message_id=CASE WHEN state='CLOSED' THEN NULL ELSE telegram_message_id END,hint=CASE WHEN state='CLOSED' THEN '' ELSE hint END,failures=CASE WHEN state='CLOSED' THEN 0 ELSE failures END,retry_after=CASE WHEN state='CLOSED' THEN 0 ELSE retry_after END,busy_until=CASE WHEN state='CLOSED' THEN 0 ELSE busy_until END,updated_at=excluded.updated_at",
                (self.account, owner, chat_id, secrets.token_hex(12), now()),
            )

    def row(self, token, owner):
        rows = self.store.rows(
            "SELECT * FROM chat_cards WHERE token=? AND owner_id=? AND account_id=?",
            (token, owner, self.account),
        )
        if not rows or rows[0]["state"] == "CLOSED":
            raise Conflict("Карточка закрыта или устарела. Откройте переписку в разделе «Сообщения»")
        return rows[0]

    def render(self, row):
        messages = self.store.rows(
            "SELECT * FROM chat_messages WHERE chat_id=? ORDER BY COALESCE(created_at,'') DESC,id DESC LIMIT 80",
            (row["chat_id"],),
        )
        name = self.store.setting("ui:chat:" + row["chat_id"] + ":name") or "покупателем"
        named = name not in {"покупателем", "Собеседник не указан"}
        blocks = []
        budget = 3200
        for entry in messages:
            payload = json.loads(entry["payload"])
            user = payload.get("user") or {}
            mine = user.get("id") == row["account_id"] and bool(row["account_id"])
            if not named and not mine and user.get("username"):
                name = user["username"]
                named = True
            text = (
                "[сообщение удалено]"
                if payload.get("deleted_at")
                else payload.get("text") or ("[фотография]" if payload.get("images") else "[событие]")
            )
            stamp = local_time(entry["created_at"]) if entry["created_at"] else "время не получено"
            if payload.get("local_confirmed_at"):
                stamp = "подтверждено " + stamp
            block = f"[{stamp}] <b>{'Вы' if mine else 'Покупатель'}:</b> {escaped(text, min(2200, budget))}"
            # Budget escaped HTML, also bounded below Telegram's UTF16 limit.
            units = len(block.encode("utf-16-le")) // 2
            if units > budget:
                block = f"<b>{'Вы' if mine else 'Покупатель'}:</b> {escaped(text, max(10, budget // 8))}"
                units = len(block.encode("utf-16-le")) // 2
            if units > budget or budget < 100:
                break
            blocks.append(block)
            budget -= units + 2
        text = (
            "💬 <b>Переписка с покупателем</b>\n👤 "
            + escaped(name, 70)
            + "\n\n"
            + "\n\n".join(reversed(blocks))
        )
        hint = row["hint"]
        if row["reply_operation"]:
            op = self.store.rows("SELECT status FROM outbox WHERE id=?", (row["reply_operation"],))
            if op:
                hint = (
                    {
                        "SUCCEEDED": "✅ Ответ подтверждён Playerok",
                        "UNCERTAIN": "⚠️ Результат отправки неизвестен. Повтор заблокирован до сверки.",
                        "PERMANENT_FAILED": "❌ Ответ не отправлен. Проверьте операцию в инструментах.",
                    }.get(op[0]["status"], "⏳ Отправляем ответ…")
                    if not hint
                    else hint
                )
        if hint:
            text += "\n\n" + escaped(hint, 300)
        return text, keyboard(
            [[("✉️ Ответить", "cc:reply:" + row["token"]), ("✖️ Закрыть", "cc:close:" + row["token"])]],
            home=False,
        )

    def confirmed_replies(self):
        for op in self.store.rows(
            "SELECT * FROM outbox WHERE kind='reply' AND status='SUCCEEDED' AND substr(operation_key,1,?)=?",
            (len("chat-card-reply:" + self.account + ":"), "chat-card-reply:" + self.account + ":"),
        ):
            receipt = json.loads(op["result"] or "{}")
            ident = receipt.get("message_id")
            if not ident or self.store.rows("SELECT 1 FROM chat_messages WHERE id=?", (ident,)):
                continue
            payload = json.loads(op["payload"])
            confirmed_at = now()
            message = {
                "id": ident,
                "text": payload["text"],
                "user": {"id": self.account},
                "local_confirmed_at": confirmed_at,
            }
            # This is a confirmed send receipt; exact server time will arrive via
            # normal API/WS synchronization. Do not invent a server timestamp.
            self.store.execute(
                "INSERT OR IGNORE INTO chat_messages VALUES(?,?,?,?)",
                (ident, op["target"], confirmed_at, encode(message)),
            )

    async def flush(self):
        async with self.store.chat_card_lock:
            # Expired SENDING after a crash is not proof that HTTP never ran.
            with self.store.transaction() as db:
                expired = db.execute(
                    "SELECT owner_id,chat_id FROM chat_cards WHERE state='SENDING' AND busy_until<=?",
                    (time.time(),),
                ).fetchall()
                db.execute(
                    "UPDATE chat_cards SET state='UNCERTAIN',busy_until=0 WHERE state='SENDING' AND busy_until<=?",
                    (time.time(),),
                )
                for row in expired:
                    self.unknown_notice(db, row)
            self.confirmed_replies()
            started = time.monotonic()
            for row in self.store.rows(
                "SELECT * FROM chat_cards WHERE account_id=? AND state IN ('NEW','OPEN') AND busy_until<=? AND retry_after<=? AND failures<8 ORDER BY CASE state WHEN 'NEW' THEN 0 ELSE 1 END,updated_at LIMIT 100",
                (self.account, time.time(), time.time()),
            ):
                if time.monotonic() - started >= 2:
                    break  # Budget between RPCs, never cancel a possible send.
                await self.update(row)

    async def refresh(self, owner, token):
        rows = self.store.rows(
            "SELECT * FROM chat_cards WHERE token=? AND owner_id=? AND account_id=? AND state='OPEN'",
            (token, owner, self.account),
        )
        if rows:
            await self.update(rows[0])

    @staticmethod
    def unknown_notice(db, row):
        db.execute(
            "UPDATE notifications SET status='UNCERTAIN' WHERE owner_id=? AND status='CARD' AND event_key IN (SELECT 'message:'||id FROM chat_messages WHERE chat_id=?)",
            (row["owner_id"], row["chat_id"]),
        )

    async def update(self, row):
        from app.telegram.navigation_context import panel_key

        if not hasattr(self.store, "navigation_locks"):
            self.store.navigation_locks = {}
        bot_id = getattr(self.bot, "id", None)
        key = (
            panel_key(self.store, bot_id, row["owner_id"], row["telegram_message_id"])
            if row["telegram_message_id"]
            else "chatcard:" + row["token"]
        )
        lock = self.store.navigation_locks.setdefault((bot_id, row["owner_id"], key), asyncio.Lock())
        async with lock:
            if row["telegram_message_id"]:
                panel = self.store.setting(
                    panel_key(self.store, bot_id, row["owner_id"], row["telegram_message_id"]), {}
                )
                if panel.get("closed") or panel.get("route") not in {None, "chat:" + row["chat_id"]}:
                    return
            return await self.update_card(row)

    async def update_card(self, row):
        text, markup = self.render(row)
        fingerprint = hashlib.sha256((text + markup.model_dump_json()).encode()).hexdigest()
        if row["state"] == "OPEN" and row["fingerprint"] == fingerprint:
            self.store.execute("UPDATE chat_cards SET updated_at=? WHERE token=?", (now(), row["token"]))
            return
        token = secrets.token_hex(12)
        sending = row["state"] == "NEW"
        changed = self.store.execute(
            "UPDATE chat_cards SET busy_token=?,busy_until=?,state=? WHERE token=? AND state=? AND busy_until<=?",
            (
                token,
                time.time() + 45,
                "SENDING" if sending else "OPEN",
                row["token"],
                row["state"],
                time.time(),
            ),
        ).rowcount
        if not changed:
            return
        tracking = panel_owner.set(None)
        try:
            if sending:
                result = await self.bot.send_message(row["owner_id"], text, reply_markup=markup)
                ident = result.message_id
            else:
                await self.bot.edit_message_text(
                    text, chat_id=row["owner_id"], message_id=row["telegram_message_id"], reply_markup=markup
                )
                ident = row["telegram_message_id"]
        except asyncio.CancelledError:
            with self.store.transaction() as db:
                changed = db.execute(
                    "UPDATE chat_cards SET state=?,busy_until=0,retry_after=? WHERE token=? AND busy_token=? AND state!='CLOSED'",
                    ("UNCERTAIN" if sending else "OPEN", time.time() + 30, row["token"], token),
                ).rowcount
                if sending and changed:
                    self.unknown_notice(db, row)
            raise
        except Exception as error:  # noqa: BLE001 -- never repeat an unknown send
            code = telegram_error(error)
            if not sending and code == "MESSAGE_NOT_MODIFIED":
                ident = row["telegram_message_id"]
            elif not sending and code in {"MESSAGE_NOT_FOUND", "MESSAGE_NOT_EDITABLE"}:
                self.store.execute(
                    "UPDATE chat_cards SET state='NEW',token=?,telegram_message_id=NULL,fingerprint=NULL,busy_until=0,busy_token=NULL,failures=failures+1 WHERE token=? AND busy_token=? AND state='OPEN'",
                    (secrets.token_hex(12), row["token"], token),
                )
                return
            else:
                outcome, _, delay = send_failure(error)
                rejected = outcome == "PENDING"
                retryable = (
                    rejected or not sending and code in {"TIMEOUT", "NETWORK_ERROR", "PROXY_UNAVAILABLE"}
                )
                with self.store.transaction() as db:
                    changed = db.execute(
                        "UPDATE chat_cards SET state=?,busy_until=0,retry_after=?,failures=? WHERE token=? AND busy_token=? AND state!='CLOSED'",
                        (
                            "NEW" if sending and rejected else "UNCERTAIN" if sending else "OPEN",
                            time.time() + max(min(60, 5 * 2 ** min(row["failures"], 8)), delay),
                            min(7, row["failures"] + 1)
                            if retryable
                            else 8
                            if code in REJECTED_CONTENT
                            else row["failures"] + 1,
                            row["token"],
                            token,
                        ),
                    ).rowcount
                    self.store.audit(row["owner_id"], "CHAT_CARD", row["chat_id"], code)
                    if sending and not rejected and changed:
                        self.unknown_notice(db, row)
                return
        finally:
            panel_owner.reset(tracking)
        saved = self.store.execute(
            "UPDATE chat_cards SET state='OPEN',telegram_message_id=?,fingerprint=?,busy_until=0,busy_token=NULL,retry_after=0,failures=0,updated_at=? WHERE token=? AND busy_token=? AND state!='CLOSED'",
            (ident, fingerprint, now(), row["token"], token),
        ).rowcount
        if saved:
            self.store.execute(
                "UPDATE notifications SET status='SUCCEEDED',telegram_message_id=? WHERE owner_id=? AND status='CARD' AND event_key IN (SELECT 'message:'||id FROM chat_messages WHERE chat_id=?)",
                (ident, row["owner_id"], row["chat_id"]),
            )
        elif sending:
            try:
                await self.bot.delete_message(row["owner_id"], ident)
            except Exception as error:  # noqa: BLE001 -- only this known late card
                self.store.audit(
                    row["owner_id"], "CHAT_CARD_LATE_CLOSE", row["chat_id"], telegram_error(error)
                )

    async def observe(self, token, owner, message_id):
        from app.telegram.navigation_context import panel_key

        if self.store.setting(panel_key(self.store, self.bot.id, owner, message_id), {}).get("route"):
            raise Conflict("Старая кнопка переписки устарела: в этом сообщении открыт другой экран")
        row = self.row(token, owner)
        if row["state"] in {"SENDING", "UNCERTAIN"}:
            self.store.execute(
                "UPDATE chat_cards SET state='OPEN',telegram_message_id=?,busy_until=0 WHERE token=? AND state IN ('SENDING','UNCERTAIN')",
                (message_id, token),
            )
        elif row["telegram_message_id"] != message_id:
            raise Conflict("Кнопка от старой карточки. Откройте переписку заново")
        self.store.execute("UPDATE chat_cards SET retry_after=0,failures=0 WHERE token=?", (token,))
        return self.row(token, owner)

    async def close(self, token, owner, message_id):
        rows = self.store.rows(
            "SELECT * FROM chat_cards WHERE token=? AND owner_id=? AND account_id=?",
            (token, owner, self.account),
        )
        if not rows or rows[0]["state"] == "CLOSED":
            return
        row = await self.observe(token, owner, message_id)
        self.store.execute(
            "UPDATE chat_cards SET state='CLOSED',hint='',busy_until=0 WHERE token=?", (token,)
        )
        try:
            await self.bot.delete_message(owner, row["telegram_message_id"])
        except Exception as error:  # noqa: BLE001 -- local close is durable; no blind delete loop
            self.store.audit(owner, "CHAT_CARD_CLOSE", row["chat_id"], telegram_error(error))

    async def compose(self, token, owner, message_id, state, api):
        from app.telegram.presentation import chat_peer
        from app.telegram.storage import replace_context

        row = await self.observe(token, owner, message_id)
        chat = await api.call("get_chat", chat_id=row["chat_id"])
        if chat.get("id") != row["chat_id"] or chat.get("is_texting_allowed") is not True:
            raise Conflict("Playerok не разрешает ответ в этом диалоге")
        previous = (await state.get_data()).get("chat_card")
        await replace_context(
            state,
            "chatcard:reply",
            {"chat_card": token, "chat_account": self.account, "chat_id": row["chat_id"]},
        )
        if previous and previous != token:
            self.store.execute(
                "UPDATE chat_cards SET hint='' WHERE token=? AND owner_id=?", (previous, owner)
            )
        peer = chat_peer(chat, self.account)
        if peer == "Собеседник не указан":
            names = self.store.rows(
                "SELECT json_extract(payload,'$.user.username') AS name FROM chat_messages WHERE chat_id=? AND json_extract(payload,'$.user.id')!=? AND json_extract(payload,'$.user.username') IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                (row["chat_id"], self.account),
            )
            peer = names[0]["name"] if names else ""
        self.store.execute(
            "UPDATE chat_cards SET hint=? WHERE token=?",
            ("Напишите ответ покупателю " + str(peer)[:70] + ". Отмена: /cancel", token),
        )
        await self.refresh(owner, token)

    async def compose_view(self, owner, chat_id, message_id, state, api):
        from app.telegram.presentation import chat_peer
        from app.telegram.storage import replace_context

        if (
            not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,))
            or api.account.id != self.account
        ):
            raise Conflict("Владелец или аккаунт переписки не подтверждён")
        chat = await api.call("get_chat", chat_id=chat_id)
        if chat.get("id") != chat_id or chat.get("is_texting_allowed") is not True:
            raise Conflict("Playerok не разрешает ответ в этом диалоге")
        await replace_context(
            state,
            "chatcard:reply",
            {"chat_view_message_id": message_id, "chat_account": self.account, "chat_id": chat_id},
        )
        return "Напишите ответ покупателю " + chat_peer(chat, self.account) + ". Отмена: /cancel", keyboard(
            [], back=("Отмена", "chat:" + chat_id)
        )

    async def reply(self, message, state, api):
        data = await state.get_data()
        view = data.get("chat_view_message_id")
        row = (
            {"chat_id": data.get("chat_id")}
            if view
            else self.row(data.get("chat_card"), message.from_user.id)
        )
        if data.get("chat_account") != self.account or api.account.id != self.account:
            raise Conflict("Подключённый аккаунт изменился. Ответ не отправлен")
        if not message.text:
            raise Conflict("Отправьте текстовый ответ или /cancel")
        chat = await api.call("get_chat", chat_id=row["chat_id"])
        if (
            chat.get("id") != row["chat_id"]
            or chat.get("is_texting_allowed") is not True
            or not api.account.writes_enabled
        ):
            raise Conflict("Playerok не разрешает отправку. Текст сохранён в вашем сообщении")
        key = f"chat-card-reply:{self.account}:{message.from_user.id}:{message.message_id}"
        with self.store.transaction() as db:
            current = row if view else self.row(row["token"], message.from_user.id)
            if getattr(state.storage, "store", None) is not self.store:
                raise Conflict("Нужна сохранённая сессия интерфейса. Откройте переписку заново")
            if db.execute(
                "SELECT 1 FROM outbox WHERE kind='reply' AND target=? AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') AND operation_key<>?",
                (row["chat_id"], key),
            ).fetchone():
                raise Conflict("Предыдущий ответ ещё не подтверждён. Новая отправка заблокирована до сверки")
            ident = Replies(self.store).enqueue(
                message.from_user.id,
                key,
                {"kind": "reply", "target": current["chat_id"], "payload": {"text": message.text}},
            )
            db.execute(
                "UPDATE chat_cards SET reply_operation=?,hint='' WHERE chat_id=? AND owner_id=? AND account_id=?",
                (ident, row["chat_id"], message.from_user.id, self.account),
            )
            state.storage.consume(db, state.key, "chatcard:reply", data)
        if not view:
            await self.refresh(message.from_user.id, row["token"])
        return ident
