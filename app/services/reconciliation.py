import hashlib
import hmac
import json
from datetime import datetime

from app.database.store import Conflict, encode, now
from app.playerok.adapter import validate_page


def signature(action):
    return hashlib.sha256(
        encode(
            {key: action[key] for key in ("id", "kind", "target", "payload", "created_at", "status")}
        ).encode()
    ).hexdigest()


class Reconciliation:
    """Positive read evidence + explicit owner decision; never requeue unknown writes."""

    def __init__(self, store, api):
        self.store, self.api = store, api

    @staticmethod
    def require_unclaimed_message(db, ident, message_id):
        claimed = db.execute(
            "SELECT 1 FROM outbox WHERE id<>? AND json_extract(result,'$.message_id')=? "
            "UNION ALL SELECT 1 FROM outbox_late_results WHERE operation_id<>? AND json_extract(result,'$.message_id')=? LIMIT 1",
            (ident, message_id, ident, message_id),
        ).fetchone()
        if claimed:
            raise Conflict(
                "Это сообщение уже связано с другой операцией; результат текущей остаётся неизвестным"
            )

    def owner(self, actor):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (actor,)):
            raise Conflict("Доступ закрыт")

    def action(self, actor, ident, *, uncertain=False):
        self.owner(actor)
        rows = self.store.rows("SELECT * FROM outbox WHERE id=?", (ident,))
        if not rows or (
            uncertain
            and rows[0]["status"] != "UNCERTAIN"
            and rows[0]["error_code"] != "MANUALLY_STOPPED_UNCERTAIN"
        ):
            raise Conflict("Операция недоступна или уже разрешена")
        return rows[0]

    def retry_checks(self, actor, ident, expected):
        self.owner(actor)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (ident,)).fetchone()
            if (
                not row
                or signature(dict(row)) != expected
                or row["started_at"] is not None
                or row["status"] != "PERMANENT_FAILED"
                or row["kind"] not in {"delivery", "delivery_text", "chain_message", "reply", "mark_read"}
            ):
                raise Conflict("Повтор запрещён: отправка начиналась или операция изменилась")
            db.execute(
                "UPDATE outbox SET status='RETRYABLE_FAILED',attempts=0,retry_at=0,error_code=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                (ident,),
            )
            db.execute(
                "UPDATE delivery_chains SET status='ACTIVE' WHERE order_id=? AND status='BLOCKED' AND NOT EXISTS(SELECT 1 FROM delivery_chain_steps s JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? AND o.status='UNCERTAIN')",
                (row["target"], row["target"]),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'RETRY_PREFLIGHT',?,'NOT_DISPATCHED',?)",
                (actor, ident, now()),
            )

    async def evidence(self, actor, ident, message_id=None):
        action = self.action(actor, ident, uncertain=True)
        kind, target = action["kind"], action["target"]
        payload = json.loads(action["payload"])
        if kind in {"stock_pause", "stock_resume"}:
            from app.services.logical_products import LogicalProducts

            logical = LogicalProducts(self.store, self.api)
            logical.authority(actor, payload["account_id"])
            observed = await self.api.call("get_item_visibility", id=target)
            if (
                observed.get("id") != target
                or (observed.get("user") or {}).get("id") != payload["account_id"]
            ):
                raise Conflict("Текущее состояние объявления не подтверждено его владельцем")
            labels = {"APPROVED": "в продаже", "DISCONTINUED": "снят с продажи", "SOLD": "продан"}
            return {
                "decision": "CURRENT_STATE_ONLY",
                "remote_id": target,
                "checked_at": now(),
                "reason": "Сейчас товар: "
                + labels.get(observed.get("status"), "статус не подтверждён")
                + ". Это чтение не доказывает результат прежнего запроса. Повтор запроса и автоматическое восстановление по этой операции запрещены; запись остаётся на проверке.",
            }
        if kind == "edit":
            from app.services.edit_confirmation import EditConfirmation
            from app.services.logical_products import LogicalProducts

            logical = LogicalProducts(self.store, self.api)
            logical.authority(actor)
            structured = {**action, "payload": payload}
            if payload.get("logical_binding") or payload.get("code_migration"):
                tag = "logical_binding" if payload.get("logical_binding") else "code_migration"
                logical.product(payload[tag]["product_id"], actor)
                observed = await self.api.call("get_item", id=target)
                if tag == "logical_binding":
                    receipt = logical.confirm_binding(structured, observed, writes=False)
                else:
                    from app.services.code_migration import CodeMigration

                    receipt = CodeMigration(self.store, self.api).confirm(structured, observed, writes=False)
                return {
                    "decision": "CONFIRM_READ_STATE",
                    "remote_id": target,
                    tag: receipt,
                    "checked_at": now(),
                }
            return await EditConfirmation(self.store, self.api).evidence(structured)
        if kind == "create":
            from app.services.logical_products import LogicalProducts

            draft = self.store.get_draft(target, actor)
            value = draft["payload"]
            candidate = self.store.setting("create:candidate:" + ident, {})
            if (
                not value.get("managed_product_id")
                or not candidate.get("id")
                or candidate.get("account") != self.api.account.id
            ):
                raise Conflict(
                    "Нет подтверждённого ID ответа создания. Исход неизвестен; повтор создания запрещён"
                )
            observed = await self.api.call("get_item", id=candidate["id"])
            if (
                observed.get("id") != candidate["id"]
                or [a.get("id") for a in observed.get("attachments") or []] != candidate["attachments"]
            ):
                raise Conflict("Фотографии или ID созданного товара не подтверждены")
            proof = LogicalProducts(self.store, self.api).creation_proof(actor, value, observed)
            return {
                "decision": "CONFIRM_READ_STATE",
                "remote_id": candidate["id"],
                "creation": proof,
                "checked_at": now(),
            }
        if kind == "edit_fields":
            from types import SimpleNamespace

            from app.services.item_receipts import field_receipt_matches
            from app.services.products import Products, validate_field

            draft = self.store.get_draft(payload["draft_id"], actor)
            value = draft["payload"]
            observed = await self.api.call("get_item", id=target)
            if observed.get("id") != target or (observed.get("user") or {}).get("id") != self.api.account.id:
                raise Conflict("Владелец товара не подтверждён")
            details = await Products(self.store, self.api).schema_details(
                value["category_id"], value["obtaining_type_id"], auto_agreements=False
            )
            attributes, fields, comment = {}, [], None
            for field in details["form"]:
                selected = validate_field(field, value["fields"].get(field["key"], ""))
                if field.get("comment"):
                    comment = selected
                elif field.get("data_field"):
                    fields.append(SimpleNamespace(id=field["key"], value=selected))
                else:
                    attributes[field["key"]] = selected
            if not field_receipt_matches(target, observed, attributes, fields, comment):
                raise Conflict("Характеристики не подтверждены. Неизвестная запись не повторяется")
            return {"decision": "CONFIRM_READ_STATE", "remote_id": target, "checked_at": now()}
        if kind in {"premium_publish", "premium_bump"}:
            from app.services.paid_promotions import PaidPromotions

            receipt = await PaidPromotions(self.store, self.api).reconcile(action)
            return {"decision": "CONFIRM_READ_STATE", **receipt, "checked_at": now()}
        if kind == "fulfill":
            from app.services.fulfillment import Fulfillment

            confirmed = await Fulfillment(self.store, self.api).result(action)
            if not confirmed:
                raise Conflict("Выполнение и требуемый отчёт не подтверждены; повтор запрещён")
            return {"decision": "CONFIRM_READ_STATE", **confirmed, "checked_at": now()}
        if kind == "mark_read":
            chat = await self.api.call("get_chat", chat_id=target)
            if chat.get("id") != target or chat.get("unread_messages_counter") != 0:
                raise Conflict("Чат не подтверждён как прочитанный")
            return {
                "decision": "CONFIRM_READ_STATE",
                "chat_id": target,
                "unread_messages_counter": 0,
                "checked_at": now(),
            }
        if kind not in {"reply", "delivery", "delivery_text", "chain_message"}:
            raise Conflict(
                "Для этой операции доступна остановка без повтора; успешность автоматически не устанавливается"
            )
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 100:
            raise ValueError("Укажите ID сообщения Playerok, которое лично проверили")
        account_id = getattr(self.api.account, "id", None)
        if not account_id:
            raise Conflict("Нужна авторизованная сессия Playerok для проверки отправителя")
        text, count = payload.get("text", ""), len(payload.get("photos", []))
        chat_id = target
        if kind in {"delivery_text", "chain_message"}:
            order = await self.api.call("get_deal", deal_id=target)
            chat_id = (order.get("chat") or {}).get("id")
            if order.get("id") != target or order.get("direction") != "OUT" or not chat_id:
                raise Conflict("Чат продажи не подтверждён")
        if kind == "delivery":
            from app.services.logical_products import stock_scope

            order = await self.api.call("get_deal", deal_id=target)
            stock = self.store.rows(
                "SELECT i.id,i.secret,i.item_id FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id=?",
                (target,),
            )
            chat_id = (order.get("chat") or {}).get("id")
            if (
                not stock
                or order.get("id") != target
                or order.get("direction") != "OUT"
                or not chat_id
                or stock[0]["id"] != payload.get("inventory_id")
                or stock[0]["item_id"] != stock_scope(self.store, (order.get("item") or {}).get("id"), target)
            ):
                raise Conflict("Резерв не соответствует актуальной продаже")
            text = stock[0]["secret"]
        cursor, seen = None, set()
        for _ in range(20):
            page = await self.api.call("get_chat_messages", chat_id=chat_id, count=24, after_cursor=cursor)
            messages, following = validate_page(page, "messages", seen)
            match = next((message for message in messages if message["id"] == message_id.strip()), None)
            if match:
                if (
                    match.get("deleted_at")
                    or (match.get("user") or {}).get("id") != account_id
                    or not hmac.compare_digest((match.get("text") or "").encode(), text.encode())
                ):
                    raise Conflict("ID найден, но отправитель, текст или доступность не совпадают")
                try:
                    created = datetime.fromisoformat(match["created_at"])
                    queued = datetime.fromisoformat(action["created_at"]).replace(microsecond=0)
                    if created.tzinfo is None or created < queued:
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    raise Conflict("Сообщение старше операции или время не подтверждено") from None
                images = match.get("images") or []
                if not isinstance(images, list) or len(images) != count:
                    raise Conflict("Количество вложений не совпадает")
                with self.store.transaction() as db:
                    self.require_unclaimed_message(db, ident, match["id"])
                return {
                    "decision": "CONFIRM_MESSAGE",
                    "chat_id": chat_id,
                    "message_id": match["id"],
                    "image_count": count,
                    "image_content_verified": False,
                    "text_matches": True,
                    "sender_matches": True,
                    "checked_at": now(),
                }
            if following is None:
                break
            cursor = following
            seen.add(cursor)
        raise Conflict(
            "Сообщение не найдено в проверенных страницах. Это не доказывает отсутствие отправки; повтор не разрешён"
        )

    async def resolve(self, actor, ident, expected, decision, reason, message_id=None, *, automatic=False):
        action = self.action(actor, ident, uncertain=True)
        if automatic and (
            action["kind"] not in {"premium_publish", "premium_bump"}
            or decision != "CONFIRM_READ_STATE"
            or action["status"] != "UNCERTAIN"
            or not self.store.setting("promotions:intent:" + ident, {}).get("automatic_receipt_check")
            or self.store.rows("SELECT 1 FROM operation_resolutions WHERE operation_id=?", (ident,))
            or self.store.setting("promotions:review:" + ident)
        ):
            raise Conflict("Автоматическая сверка этой операции не разрешена")
        if signature(action) != expected:
            raise Conflict("Операция изменилась. Откройте её заново")
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
            raise ValueError("Комментарий к решению: от 10 до 500 символов; без данных выдачи и секретов")
        if decision == "STOP_UNCERTAIN":
            evidence = {"external_outcome": "UNKNOWN", "checked_at": now()}
            status, error = "PERMANENT_FAILED", "MANUALLY_STOPPED_UNCERTAIN"
        elif decision in {"CONFIRM_MESSAGE", "CONFIRM_READ_STATE"}:
            evidence = await self.evidence(actor, ident, message_id)
            if evidence["decision"] != decision:
                raise Conflict("Тип решения не соответствует операции")
            status, error = "SUCCEEDED", None
        else:
            raise ValueError("Недопустимое решение")
        result = {
            **evidence,
            "resolution_source": "SERVER_RECEIPT" if automatic else "OWNER_DECISION",
            "actor": actor,
            "decision": decision,
        }
        with self.store.transaction() as db:
            current = db.execute("SELECT * FROM outbox WHERE id=?", (ident,)).fetchone()
            reconfirm = current["error_code"] == "MANUALLY_STOPPED_UNCERTAIN" and decision != "STOP_UNCERTAIN"
            if (
                not current
                or (current["status"] != "UNCERTAIN" and not reconfirm)
                or signature(dict(current)) != expected
            ):
                raise Conflict("Операция уже разрешена другим действием")
            if decision == "STOP_UNCERTAIN" and action["kind"] == "premium_bump":
                from app.services.premium_review import release_ids

                if ident in release_ids(self.store):
                    raise Conflict("Расход уже учтён. Результат старой покупки остаётся неизвестным")
            if action["kind"] == "create" and status == "SUCCEEDED":
                from app.services.logical_products import LogicalProducts

                draft = self.store.get_draft(action["target"], actor)
                LogicalProducts(self.store, self.api).commit_creation_proof(
                    db, actor, draft["id"], draft["payload"], evidence["creation"]
                )
            if action["kind"] in {"premium_publish", "premium_bump"} and status == "SUCCEEDED":
                from app.services.paid_promotions import PaidPromotions, finish_expense, intent_key

                value = json.loads(action["payload"])
                expense = db.execute(
                    "SELECT item_id,amount,status FROM promotion_expenses WHERE operation_key=?",
                    (action["operation_key"],),
                ).fetchone()
                from app.services.finance import money

                if (
                    not expense
                    or expense["item_id"] != action["target"]
                    or money(expense["amount"]) != money(value["amount"])
                    or expense["status"] != "UNCERTAIN"
                    or self.api.account.id != value["account_id"]
                    or self.store.setting("playerok:account_id", value["account_id"]) != value["account_id"]
                    or not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (actor,)).fetchone()
                    or automatic
                    and (
                        self.store.setting("promotions:review:" + ident)
                        or db.execute(
                            "SELECT 1 FROM operation_resolutions WHERE operation_id=?", (ident,)
                        ).fetchone()
                    )
                ):
                    raise Conflict("Финансовый журнал, аккаунт или решение владельца изменились")
                # Evidence preparation can race another confirmation. The exact
                # receipt claim and both ledger transitions share this COMMIT.
                PaidPromotions(self.store, self.api).unused_receipt(evidence["payment_id"], ident)
                intent = self.store.setting(intent_key(ident), {})
                self.store.set_setting(
                    intent_key(ident),
                    {
                        **intent,
                        "candidate_id": evidence["payment_id"],
                        "receipt_id": evidence["payment_id"],
                        "pending": False,
                    },
                )
                finish_expense(db, ident, "SUCCEEDED")
            if action["kind"] in {"premium_publish", "premium_bump"} and decision == "STOP_UNCERTAIN":
                from app.services.paid_promotions import finish_expense, intent_key

                if not self.store.setting(intent_key(ident), {}).get("started_at"):
                    # The invariant writes intent BEFORE any paid request. A lost lease cannot cross this guard.
                    finish_expense(db, ident, "FAILED")
                    # This is stronger than an owner decision: intent.started_at is
                    # durably recorded before RPC. Share the same atomic, revision-
                    # guarded release used by a proven local publication failure.
                    if action["kind"] == "premium_publish":
                        self.store.release_launch(
                            db,
                            ident,
                            action["target"],
                            action["payload"],
                            "PERMANENT_FAILED",
                            error="VALIDATION_FAILED",
                        )
            if decision == "CONFIRM_MESSAGE":
                self.require_unclaimed_message(db, ident, evidence["message_id"])
            if action["kind"] == "delivery" and status == "SUCCEEDED":
                reservation = db.execute(
                    "SELECT i.id,i.status FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id=?",
                    (action["target"],),
                ).fetchone()
                if (
                    not reservation
                    or reservation["id"] != json.loads(action["payload"]).get("inventory_id")
                    or reservation["status"] != "QUARANTINED"
                ):
                    raise Conflict("Резерв изменился; выдача не может быть подтверждена")
            db.execute(
                "INSERT INTO operation_resolutions VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(operation_id) DO UPDATE SET actor=excluded.actor,decision=excluded.decision,"
                "reason=excluded.reason,evidence=excluded.evidence,previous_error_code=excluded.previous_error_code,"
                "previous_result=excluded.previous_result,resolved_at=excluded.resolved_at",
                (
                    ident,
                    actor,
                    decision,
                    reason.strip(),
                    encode(evidence),
                    current["error_code"],
                    current["result"],
                    now(),
                ),
            )
            # Owner resolution of a launch publication NEVER releases the launch:
            # STOP_UNCERTAIN only stops retries; it does not prove the external request
            # was never sent (the item may already be published). CONFIRM_* success
            # keeps the draft QUEUED as the completed launch.
            db.execute(
                "UPDATE outbox SET status=?,result=?,error_code=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                (status, encode(result), error, ident),
            )
            if action["kind"] == "edit_fields" and status == "SUCCEEDED":
                db.execute(
                    "UPDATE drafts SET state='UPDATED' WHERE id=? AND state='QUEUED'",
                    (json.loads(action["payload"])["draft_id"],),
                )
            if action["kind"] == "edit" and status == "SUCCEEDED":
                from app.services.edit_confirmation import finish_edit

                structured = {**action, "payload": json.loads(action["payload"])}
                if structured["payload"].get("logical_binding"):
                    from app.services.logical_products import LogicalProducts

                    LogicalProducts(self.store, self.api).binding_guard(structured, writes=False)
                if structured["payload"].get("code_migration"):
                    from app.services.code_migration import CodeMigration

                    CodeMigration(self.store, self.api).guard(structured, writes=False)
                finish_edit(db, structured, result)
            if action["kind"] == "delivery" and status == "SUCCEEDED":
                db.execute(
                    "UPDATE inventory SET status='DELIVERED' WHERE id=(SELECT inventory_id FROM reservations WHERE order_id=?) AND status='QUARANTINED'",
                    (action["target"],),
                )
            if status == "SUCCEEDED" and decision == "CONFIRM_MESSAGE":
                chain = db.execute(
                    "SELECT policy FROM delivery_chains WHERE order_id=? AND status='BLOCKED'",
                    (action["target"],),
                ).fetchone()
                steps = db.execute(
                    "SELECT o.status FROM delivery_chain_steps s LEFT JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=?",
                    (action["target"],),
                ).fetchall()
                # Finish a fully verified sequence, never resume pending/unknown sends.
                if chain and steps and all(step["status"] == "SUCCEEDED" for step in steps):
                    state = (
                        "NEEDS_FULFILLMENT"
                        if json.loads(chain["policy"]).get("mark_sent")
                        and json.loads(chain["policy"]).get("mode") != "playerok"
                        else "SUCCEEDED"
                    )
                    db.execute(
                        "UPDATE delivery_chains SET status=? WHERE order_id=? AND status='BLOCKED'",
                        (state, action["target"]),
                    )
            # Owner resolution NEVER releases a launch: an owner decision cannot prove
            # the external publication request was never sent. STOP_UNCERTAIN only
            # stops retries (the draft stays QUEUED = manual attention required).
            if action["kind"] == "delivery":
                db.execute(
                    "INSERT INTO order_history(order_id,source,new_status,actor,note,created_at) SELECT id,'MANUAL_DELIVERY_RESOLUTION',status,?,?,? FROM orders WHERE id=?",
                    (actor, decision, now(), action["target"]),
                )
            if action["kind"] == "fulfill":
                db.execute(
                    "INSERT INTO order_history(order_id,source,new_status,actor,note,created_at) SELECT id,'MANUAL_FULFILLMENT_RESOLUTION',status,?,?,? FROM orders WHERE id=?",
                    (actor, decision, now(), action["target"]),
                )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (
                    None if automatic else actor,
                    "AUTO_CONFIRM_PREMIUM" if automatic else "RESOLVE_OPERATION",
                    ident,
                    decision,
                    now(),
                ),
            )
        return result
