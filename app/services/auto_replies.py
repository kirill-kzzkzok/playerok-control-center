import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.database.store import Conflict, encode, now
from app.playerok.adapter import validate_page
from app.services.products import ValidationError
from app.services.replies import reply_text
from app.services.statistics import parsed_time


def phrase(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise ValueError("Фраза: от 1 до 200 символов")
    return " ".join(value.casefold().split())


def message_signature(message):
    value = {
        key: message.get(key)
        for key in (
            "id",
            "text",
            "created_at",
            "deleted_at",
            "event",
            "is_auto_response",
            "images",
        )
    }
    value["author"] = (message.get("user") or {}).get("id")
    return hashlib.sha256(encode(value).encode()).hexdigest()


class AutoReplies:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def owner(self, actor):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (actor,)):
            raise Conflict("Доступ закрыт")

    def account(self):
        ident = getattr(self.api.account, "id", None) or self.store.setting("playerok:account_id")
        if not ident:
            raise Conflict("Аккаунт не подтверждён")
        return ident

    def rules(self, actor):
        self.owner(actor)
        return self.store.rows(
            "SELECT * FROM auto_reply_rules WHERE owner_id=? AND account_id=? ORDER BY id",
            (actor, self.account()),
        )

    def save(self, actor, values, *, ident=None, revision=0, armed=False):
        self.owner(actor)
        trigger, response = phrase(values.get("trigger_text")), reply_text(values.get("response_text"))
        enabled, cooldown, limit = (
            values.get("enabled", False),
            values.get("cooldown", 600),
            values.get("daily_limit", 20),
        )
        if (
            type(enabled) is not bool
            or type(cooldown) is not int
            or not 60 <= cooldown <= 86400
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Пауза: 60–86400 секунд; лимит: 1–100; enabled должен быть bool")
        if enabled and armed is not True:
            raise Conflict("Включение требует отдельного подтверждения будущих автоответов")
        account = self.account()
        with self.store.transaction() as db:
            if db.execute(
                "SELECT 1 FROM auto_reply_rules WHERE account_id=? AND trigger_text=? AND id<>?",
                (account, trigger, ident or ""),
            ).fetchone():
                raise Conflict("Правило с такой фразой уже существует")
            if ident is None:
                if (
                    db.execute(
                        "SELECT count(*) FROM auto_reply_rules WHERE account_id=?", (account,)
                    ).fetchone()[0]
                    >= 10
                ):
                    raise Conflict("Не более 10 правил на аккаунт")
                ident = uuid.uuid4().hex[:16]
                db.execute(
                    "INSERT INTO auto_reply_rules VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ident, actor, account, 1, int(enabled), trigger, response, cooldown, limit, now()),
                )
            elif (
                db.execute(
                    "UPDATE auto_reply_rules SET revision=revision+1,enabled=?,trigger_text=?,response_text=?,cooldown=?,daily_limit=?,updated_at=? WHERE id=? AND owner_id=? AND account_id=? AND revision=?",
                    (
                        int(enabled),
                        trigger,
                        response,
                        cooldown,
                        limit,
                        now(),
                        ident,
                        actor,
                        account,
                        revision,
                    ),
                ).rowcount
                != 1
            ):
                raise Conflict("Правило изменилось; откройте заново")
            db.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    f"auto_reply:{ident}:inbox_cutoff",
                    encode(db.execute("SELECT COALESCE(MAX(rowid),0) FROM inbox").fetchone()[0]),
                ),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'AUTO_REPLY_RULE',?,?,?)",
                (actor, ident, "ARMED_FUTURE_ONLY" if enabled else "DISABLED", now()),
            )
        return ident

    def get(self, actor, ident):
        return next((rule for rule in self.rules(actor) if rule["id"] == ident), None)

    def delete(self, actor, ident, revision):
        self.owner(actor)
        with self.store.transaction() as db:
            if (
                db.execute(
                    "DELETE FROM auto_reply_rules WHERE id=? AND owner_id=? AND account_id=? AND revision=? AND enabled=0",
                    (ident, actor, self.account(), revision),
                ).rowcount
                != 1
            ):
                raise Conflict("Сначала выключите правило и откройте его заново")
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'DELETE_AUTO_REPLY_RULE',?,'DELETED',?)",
                (actor, ident, now()),
            )

    def stop(self, actor):
        self.owner(actor)
        return self.store.execute(
            "UPDATE auto_reply_rules SET enabled=0,revision=revision+1,updated_at=? WHERE owner_id=? AND account_id=? AND enabled=1",
            (now(), actor, self.account()),
        ).rowcount

    def matches(self, message, rule, received_at, at=None):
        at = at or datetime.now(UTC)
        boundary = self.store.setting(f"auto_reply:{rule['id']}:inbox_cutoff")
        source = self.store.rows(
            "SELECT rowid AS sequence FROM inbox WHERE key=?", ("message:" + str(message.get("id")),)
        )
        if type(boundary) is not int or not source or source[0]["sequence"] <= boundary:
            return False
        created, received, cutoff = map(
            parsed_time, (message.get("created_at"), received_at, rule["updated_at"])
        )
        author = message.get("user") or {}
        if (
            not created
            or not received
            or not cutoff
            or created < cutoff
            or received < cutoff
            or not at - timedelta(minutes=5) <= created <= at + timedelta(seconds=5)
            or not author.get("id")
            or author["id"] == rule["account_id"]
            or author.get("role") != "USER"
            or message.get("event") is not None
            or message.get("deleted_at")
            or message.get("images")
            or message.get("is_auto_response") is not False
        ):
            return False
        try:
            return phrase(message.get("text")) == rule["trigger_text"]
        except ValueError:
            return False

    async def fresh(self, rule, chat_id, message_id, expected):
        chat = await self.api.call("get_chat", chat_id=chat_id)
        participants = {user.get("id") for user in (chat.get("users") or [])}
        if (
            chat.get("id") != chat_id
            or chat.get("type") != "PM"
            or chat.get("is_texting_allowed") is not True
            or rule["account_id"] not in participants
        ):
            raise ValidationError("Чат не подтверждён как доступный личный диалог")
        page = await self.api.call("get_chat_messages", chat_id=chat_id, count=24, after_cursor=None)
        entries, _ = validate_page(page, "messages")
        message = next((entry for entry in entries if entry["id"] == message_id), None)
        if (
            not message
            or message_signature(message) != expected
            or (message.get("user") or {}).get("id") not in participants
        ):
            raise ValidationError("Входящее сообщение изменилось или отсутствует в свежей странице")
        created = parsed_time(message.get("created_at"))
        for other in entries:
            if other["id"] == message_id or other.get("event") is not None:
                continue
            when = parsed_time(other.get("created_at"))
            if when is None or created is None or when >= created:
                raise ValidationError("Диалог уже продолжился; автоответ отменён")
        return message

    def limits(self, rule, chat_id, at=None):
        at = at or datetime.now(UTC)
        rows = self.store.rows(
            "SELECT * FROM auto_reply_events WHERE account_id=? AND status='QUEUED'", (rule["account_id"],)
        )
        day = at.astimezone(ZoneInfo("Europe/Moscow")).date()
        count = 0
        for row in rows:
            created = parsed_time(row["created_at"])
            if not created:
                raise Conflict("Неизвестно время прежнего автоответа")
            if row["chat_id"] == chat_id and created > at - timedelta(seconds=rule["cooldown"]):
                raise Conflict("Пауза между автоответами ещё не прошла")
            if row["rule_id"] == rule["id"] and created.astimezone(ZoneInfo("Europe/Moscow")).date() == day:
                count += 1
        if count >= rule["daily_limit"]:
            raise Conflict("Суточный лимит автоответов исчерпан")
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND kind='reply' AND (status IN ('PENDING','PROCESSING','UNCERTAIN','RETRYABLE_FAILED') OR error_code='MANUALLY_STOPPED_UNCERTAIN') LIMIT 1",
            (chat_id,),
        ):
            raise Conflict("В чате есть незавершённый или неопределённый ответ")
        self.delivery_idle(chat_id)

    def delivery_idle(self, chat_id):
        if self.store.rows(
            "SELECT 1 FROM orders d JOIN outbox o ON o.target=d.id WHERE d.chat_id=? "
            "AND o.kind IN ('delivery','delivery_text','chain_message','fulfill') "
            "AND (o.status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') OR o.error_code='MANUALLY_STOPPED_UNCERTAIN') LIMIT 1",
            (chat_id,),
        ) or self.store.rows(
            "SELECT 1 FROM delivery_chains c JOIN orders d ON d.id=c.order_id WHERE d.chat_id=? AND c.status IN ('ACTIVE','BLOCKED','NEEDS_FULFILLMENT') LIMIT 1",
            (chat_id,),
        ):
            raise ValidationError("В чате есть незавершённая выдача или исполнение заказа")

    async def consider(self, row):
        account = self.account()
        message = json.loads(row["payload"])
        rules = self.store.rows(
            "SELECT * FROM auto_reply_rules WHERE account_id=? AND enabled=1 ORDER BY id", (account,)
        )
        rule = next((rule for rule in rules if self.matches(message, rule, row["received_at"])), None)
        status, code, operation = "SKIPPED", None, None
        if rule:
            await self.fresh(rule, row["chat_id"], message["id"], message_signature(message))
        with self.store.transaction() as db:
            if db.execute(
                "SELECT 1 FROM auto_reply_events WHERE account_id=? AND message_id=?",
                (account, message["id"]),
            ).fetchone():
                return None
            if rule:
                self.owner(rule["owner_id"])
                current = db.execute("SELECT * FROM auto_reply_rules WHERE id=?", (rule["id"],)).fetchone()
                if not current or not current["enabled"] or current["revision"] != rule["revision"]:
                    raise Conflict("Правило изменилось во время проверки")
                if not getattr(self.api.account, "writes_enabled", False) or not getattr(
                    self.api.account, "automation_enabled", False
                ):
                    raise Conflict("Runtime автоответов выключен")
                self.limits(rule, row["chat_id"])
                payload = {
                    "text": rule["response_text"],
                    "auto_reply": {
                        "rule_id": rule["id"],
                        "revision": rule["revision"],
                        "account_id": account,
                        "message_id": message["id"],
                        "signature": message_signature(message),
                        "received_at": row["received_at"],
                    },
                }
                operation = self.store.enqueue(
                    "auto_reply:" + account + ":" + message["id"], "reply", row["chat_id"], payload, db=db
                )
                status = "QUEUED"
            db.execute(
                "INSERT INTO auto_reply_events(account_id,message_id,chat_id,rule_id,rule_revision,status,operation_id,error_code,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    message["id"],
                    row["chat_id"],
                    rule["id"] if rule else None,
                    rule["revision"] if rule else None,
                    status,
                    operation,
                    code,
                    now(),
                ),
            )
        return operation

    def dispatchable(self, action):
        metadata = action["payload"].get("auto_reply")
        if not metadata:
            raise ValidationError("Нет зарегистрированного правила")
        if (
            not getattr(self.api.account, "automation_enabled", False)
            or self.api.account.id != metadata["account_id"]
        ):
            raise ValidationError("Runtime или аккаунт изменился")
        from app.services.sync_health import chat_sync_ready

        if not chat_sync_ready(self.store, action["target"]):
            raise ValidationError("Нет актуальной базовой сверки")
        rows = self.store.rows("SELECT * FROM auto_reply_rules WHERE id=?", (metadata["rule_id"],))
        rule = rows[0] if rows else None
        if (
            not rule
            or not rule["enabled"]
            or rule["revision"] != metadata["revision"]
            or rule["response_text"] != action["payload"].get("text")
        ):
            raise ValidationError("Правило выключено или изменилось")
        self.owner(rule["owner_id"])
        registered = self.store.rows(
            "SELECT * FROM auto_reply_events WHERE account_id=? AND message_id=? AND operation_id=? AND status='QUEUED'",
            (metadata["account_id"], metadata["message_id"], action["id"]),
        )
        rows = self.store.rows(
            "SELECT payload FROM chat_messages WHERE id=? AND chat_id=?",
            (metadata["message_id"], action["target"]),
        )
        message = json.loads(rows[0]["payload"]) if rows else None
        if (
            not registered
            or not message
            or message_signature(message) != metadata["signature"]
            or not self.matches(message, rule, metadata["received_at"])
        ):
            raise ValidationError("Сообщение устарело или изменилось")
        created = parsed_time(message.get("created_at"))
        for row in self.store.rows(
            "SELECT id,payload FROM chat_messages WHERE chat_id=? AND id<>?",
            (action["target"], metadata["message_id"]),
        ):
            other = json.loads(row["payload"])
            when = parsed_time(other.get("created_at"))
            if other.get("event") is None and when and when >= created:
                raise ValidationError("Диалог уже продолжился после сообщения")
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND id<>? AND kind='reply' AND (status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') OR error_code='MANUALLY_STOPPED_UNCERTAIN') LIMIT 1",
            (action["target"], action["id"]),
        ):
            raise ValidationError("В чате есть другой ожидающий/неопределённый ответ")
        self.delivery_idle(action["target"])
        return rule

    async def verify(self, action):
        rule = self.dispatchable(action)
        metadata = action["payload"]["auto_reply"]
        await self.fresh(rule, action["target"], metadata["message_id"], metadata["signature"])
        self.dispatchable(action)
