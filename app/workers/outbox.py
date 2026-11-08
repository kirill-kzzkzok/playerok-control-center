import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.database.store import Conflict
from app.playerok.adapter import APIError, SafeAccount, plain
from app.playerok.thread_calls import CallsClosedError, calls_for
from app.security.logging import event
from app.services.agreements import Agreements, require_conditions
from app.services.attachments import Attachments
from app.services.auto_replies import AutoReplies
from app.services.delivery_chains import DeliveryChains
from app.services.discounts import discount_plan
from app.services.fulfillment import Fulfillment
from app.services.item_receipts import edit_receipt_matches, field_receipt_matches
from app.services.order_management import deliverable
from app.services.orders import Orders
from app.services.products import (
    Products,
    ValidationError,
    field_baseline,
    listing_price,
    listing_text,
    priority_price_basis,
    validate_draft,
    validate_field,
)
from app.services.replies import reply_payload
from playerokapi.enums import ItemDealStatuses


class Executor:
    def __init__(self, store, api, products):
        self.store, self.api, self.products = store, api, products

    def gifts(self):
        from app.gifts.mtproto import PersonalGiftSender
        from app.gifts.service import GiftService
        from app.gifts.settings import GiftSettings

        settings = getattr(self.api, "gift_settings", None) or GiftSettings.load()
        if not getattr(self.api, "gift_sender", None):
            self.api.gift_sender = PersonalGiftSender(settings)
        return GiftService(self.store, self.api, settings, self.api.gift_sender)

    @staticmethod
    def item_result(result):
        return {
            "remote_id": result["id"],
            **{
                k: result[k]
                for k in ("name", "slug", "status", "price", "raw_price", "created_at")
                if k in result
            },
        }

    def dispatch_guard(self, action):
        if action["kind"] == "edit" and self.store.rows(
            "SELECT 1 FROM audit_log WHERE action='RECOVER_UNSTARTED_EDIT' AND target=?", (action["id"],)
        ):
            from app.services.operation_outcomes import historical_edit_authority

            with self.store.transaction() as db:
                if not historical_edit_authority(db, action):
                    raise Conflict("Правка отменена последующим решением владельца")
        if action["kind"] in {"stock_pause", "stock_resume"}:
            from app.services.stock_visibility import guard

            guard(self.store, self.api, action)
            if self.store.rows("SELECT 1 FROM outbox_late_results WHERE operation_id=?", (action["id"],)):
                raise APIError("VISIBILITY_EVIDENCE_CONFLICT", uncertain=True)
        if action["kind"] == "publish" and action["payload"].get("native_relist"):
            self.products.native_relist_guard(action["payload"])
        if action["payload"].get("logical_binding"):
            from app.services.logical_products import LogicalProducts

            LogicalProducts(self.store, self.api).binding_guard(action)
        if action["kind"] == "edit" and action["payload"].get("code_migration"):
            from app.services.code_migration import CodeMigration

            CodeMigration(self.store, self.api).guard(action)
        if action["kind"] in {"publish", "premium_publish"} and not action["payload"].get("native_relist"):
            from app.services.listing_launch import ListingLaunch

            ListingLaunch(self.store, self.api, self.gifts()).guard(action)
        from app.services.agreements import AutoAgreements

        if action["kind"] == "agreements" and action["payload"].get("automatic"):
            AutoAgreements(self.store, self.api).authority(action["payload"].get("account_id"))
        elif not getattr(self.api.account, "writes_enabled", False):
            if getattr(getattr(self.api, "connection", None), "awaiting_verification", False):
                raise APIError("CONNECTION_NOT_VERIFIED")
            raise ValidationError("Записи Playerok выключены")
        rows = self.store.rows(
            "SELECT status,lease_token,lease_until FROM outbox WHERE id=?", (action["id"],)
        )
        if (
            not rows
            or rows[0]["status"] != "PROCESSING"
            or rows[0]["lease_token"] != action["lease_token"]
            or (rows[0]["lease_until"] or 0) <= time.time()
        ):
            raise ValidationError("Lease операции истёк или потерян; отправка запрещена")
        from app.services.operation_outcomes import AUTOMATIC_DELIVERY_KINDS

        if action["kind"] in AUTOMATIC_DELIVERY_KINDS and self.store.rows(
            "SELECT 1 FROM outbox_late_results WHERE operation_id=? LIMIT 1", (action["id"],)
        ):
            raise APIError("DELIVERY_PREFLIGHT_EVIDENCE_CONFLICT", uncertain=True)
        if action["kind"] in {"premium_publish", "premium_bump"}:
            from app.services.paid_promotions import PaidPromotions

            PaidPromotions(self.store, self.api).guard(action)
        if action["kind"] == "delete":
            Products(self.store, self.api).require_deletable(action["target"], action["id"])
        if action["kind"] == "edit_fields" or (
            action["kind"] == "edit"
            and "description" in action["payload"]
            and not action["payload"].get("logical_binding")
            and not action["payload"].get("code_migration")
            and not action["payload"].get("marketing_description")
        ):
            Products(self.store, self.api).require_critical_edit(action["target"], action["id"])
        if action["payload"].get("chain_id"):
            DeliveryChains(self.store, self.api).dispatchable(action)
        if action["payload"].get("paid_greeting"):
            from app.services.paid_greeting import guard

            guard(self.store, self.api, action)
        if action["payload"].get("review_thanks"):
            from app.services.review_thanks import guard

            guard(self.store, self.api, action)
        if action["payload"].get("auto_reply"):
            AutoReplies(self.store, self.api).dispatchable(action)
        if action["kind"] in {"delivery", "delivery_text", "chain_message"} and not action["payload"].get(
            "platform_followup"
        ):
            from app.services.logical_products import LogicalProducts

            local = self.store.rows("SELECT item_id FROM orders WHERE id=?", (action["target"],))
            if local:
                LogicalProducts(self.store, self.api).require_delivery(
                    local[0]["item_id"], order_id=action["target"]
                )
        if action["kind"] in {"gift_refund", "gift_notice"}:
            from app.gifts.execution import guard

            guard(self.gifts(), action)
        if action["payload"].get("gift_proof"):
            j = self.gifts().job(action["target"])
            l = self.gifts().listing(j["item_id"])
            if (
                j["state"] != "SUCCEEDED"
                or not j["receipt"]
                or j["refund_state"] != "NONE"
                or not l["enabled"]
                or not self.gifts().completion_allowed(j, l)
                or not self.gifts().runtime()
                or j["listing_revision"] != l["revision"]
            ):
                raise ValidationError("Подтверждение подарочной доставки больше не разрешает выполнение")
        if action["kind"] in {"delivery", "fulfill"} and not action["payload"].get("platform_followup"):
            orders = self.store.rows("SELECT payload,status FROM orders WHERE id=?", (action["target"],))
            if not orders or orders[0]["status"] != "PAID":
                raise ValidationError("Сохранённое состояние продажи запрещает выполнение")
            deliverable(
                json.loads(orders[0]["payload"]),
                action["target"],
                self.api.account.id,
                wait_for_details=bool(action["payload"].get("chain_id")),
            )
            if self.store.rows(
                "SELECT 1 FROM order_annotations WHERE order_id=? AND problem=1", (action["target"],)
            ):
                raise ValidationError("Локальная проблема блокирует выполнение")
        if action["kind"] == "delivery":
            reserved = self.store.rows(
                "SELECT i.status FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id=?",
                (action["target"],),
            )
            if not reserved or reserved[0]["status"] != "RESERVED":
                raise ValidationError("Резерв выдачи больше не разрешает отправку")

    async def mutate(self, action, operation, method, **kwargs):
        def run(cancelled):
            def guard():
                if cancelled.is_set():
                    raise ValidationError("Операция отменена до отправки")
                self.dispatch_guard(action)
                if (
                    action["kind"] == "agreements"
                    and action["payload"].get("automatic")
                    and isinstance(self.api.account, SafeAccount)
                ):
                    from app.services.agreements import AutoAgreements

                    AutoAgreements(self.store, self.api).before_dispatch(action)
                    # The fresh read can itself wait on the network. Recheck
                    # cancellation, account/policy and lease AFTER it, before POST.
                    if cancelled.is_set():
                        raise ValidationError("Операция отменена до отправки")
                    self.dispatch_guard(action)

            scope = (
                self.api.account.permit(
                    operation, dispatch_guard=guard, dispatch_start=lambda: self.store.mark_started(action)
                )
                if isinstance(self.api.account, SafeAccount)
                else self.api.account.permit(operation)
            )
            with scope:
                guard()
                if not isinstance(self.api.account, SafeAccount):
                    self.store.mark_started(action)
                try:
                    return plain(getattr(self.api.account, method)(**kwargs))
                except (APIError, ValidationError):
                    raise
                except Exception:  # noqa: BLE001 -- mutation may have succeeded; preserve uncertainty
                    raise APIError("MUTATION_RESULT_UNKNOWN", uncertain=True) from None

        def abandoned(result, error):
            # A raw method return is evidence, not the full executor's success predicate.
            if result is None and error is None:
                return
            receipt = {}
            if isinstance(result, dict):
                if operation == "createChatMessage" and result.get("id"):
                    receipt["message_id"] = result["id"]
                elif operation == "createItem" and result.get("id"):
                    receipt["remote_id"] = result["id"]
            self.store.record_late_result(
                action["id"],
                action["lease_token"],
                "UNCERTAIN",
                result=receipt,
                error="CANCELLED_CALL_FAILED" if error else "CANCELLED_CALL_COMPLETED",
            )

        try:
            return await calls_for(self.api).call(run, on_abandoned=abandoned)
        except CallsClosedError:
            raise ValidationError("API завершает работу; отправка не начата") from None

    async def owned_item(self, item_id):
        item = await self.api.call("get_item", id=item_id)
        if not self.api.account.id or (item.get("user") or {}).get("id") != self.api.account.id:
            raise ValidationError("Владение лотом не подтверждено")
        return item

    async def __call__(self, action):
        kind, target, payload = action["kind"], action["target"], action["payload"]
        if kind in {"stock_pause", "stock_resume"}:
            from app.services.stock_visibility import execute

            return await execute(self, action)
        from app.services.operation_outcomes import AUTOMATIC_DELIVERY_KINDS, unstarted_delivery

        if kind in AUTOMATIC_DELIVERY_KINDS:
            with self.store.transaction() as db:
                if not unstarted_delivery(db, action):
                    raise APIError("DELIVERY_PREFLIGHT_EVIDENCE_CONFLICT", uncertain=True)
        if kind in AUTOMATIC_DELIVERY_KINDS | {"gift_send"}:
            local = self.store.rows("SELECT payload FROM orders WHERE id=?", (target,))
            if local:
                saved = json.loads(local[0]["payload"])
                if saved.get("has_problem") is None or saved.get("is_automated") is None:
                    from app.services.order_management import deal_details

                    observed = await deal_details(self.api, target, need_chat=kind != "fulfill")
                    Orders(self.store).ingest(observed)
        if kind in {"publish", "premium_publish"} and not payload.get("native_relist"):
            from app.services.listing_launch import ListingLaunch

            await ListingLaunch(self.store, self.api, self.gifts()).preflight(action)
        if kind in {"premium_publish", "premium_bump"}:
            from app.services.paid_promotions import PaidPromotions

            return await PaidPromotions(self.store, self.api).execute(self, action)
        if kind == "gift_send":
            from telethon import errors as mtproto_errors

            from app.gifts.core import GiftValidationError
            from app.gifts.mtproto import GiftFailure

            service = self.gifts()
            try:
                async with asyncio.timeout(90):
                    return await service.execute(action)
            except (GiftFailure, GiftValidationError, ValidationError, Conflict) as exc:
                spending = service.job(target)["state"] in {"SPENDING", "UNCERTAIN"}
                raise APIError(getattr(exc, "code", "GIFT_VALIDATION_FAILED"), uncertain=spending) from None
            except APIError as error:
                spending = service.job(target)["state"] in {"SPENDING", "UNCERTAIN"}
                raise APIError(error.code, uncertain=spending or error.uncertain) from None
            except mtproto_errors.FloodWaitError as error:
                spending = service.job(target)["state"] in {"SPENDING", "UNCERTAIN"}
                raise APIError(
                    "GIFT_PREFLIGHT_NETWORK", uncertain=spending, retry_after=error.seconds
                ) from None
            except (TimeoutError, OSError):
                spending = service.job(target)["state"] in {"SPENDING", "UNCERTAIN"}
                raise APIError(
                    "GIFT_PAYMENT_UNKNOWN" if spending else "GIFT_PREFLIGHT_NETWORK", uncertain=spending
                ) from None
            except Exception as error:  # noqa: BLE001 -- protect RPC details and preserve paid uncertainty
                spending = service.job(target)["state"] in {"SPENDING", "UNCERTAIN"}
                event(
                    "WARNING",
                    "Ошибка личного Telegram: " + type(error).__name__,
                    order=target,
                    operation=action["id"],
                    level=30,
                )
                raise APIError(
                    "GIFT_PAYMENT_UNKNOWN" if spending else "GIFT_CHECK_FAILED", uncertain=spending
                ) from None
        if kind in {"gift_refund", "gift_notice"}:
            from app.gifts.execution import execute_followup

            return await execute_followup(self, action)
        chain_chat = None
        if payload.get("chain_id"):
            chain_chat = await DeliveryChains(self.store, self.api).verify_step(action)
        if kind in {"chain_message", "delivery_text"}:
            if not payload.get("chain_id"):
                raise ValidationError("Автоматическое сообщение требует зарегистрированной цепочки")
            text = payload.get("text", "")
            if not text.strip() or len(text.encode("utf-16-le")) // 2 > 3500:
                raise ValidationError("Пустое или слишком длинное сообщение")
            result = await self.mutate(
                action, "createChatMessage", "send_message", chat_id=chain_chat, text=text
            )
            if not result.get("id"):
                raise APIError("CHAIN_MESSAGE_UNKNOWN", uncertain=True)
            return {"message_id": result["id"]}
        if kind == "fulfill":
            service = Fulfillment(self.store, self.api)
            platform = payload.get("platform_followup") is True
            if platform and not payload.get("chain_id"):
                raise ValidationError("Нужно разрешение сохранённого сценария заказа")
            if platform:
                from app.services.platform_orders import PlatformOrders

                current = await PlatformOrders(self.store, self.api).verify(action)
                if current["status"] != "PAID":
                    confirmed = await service.result(action)
                    if confirmed:
                        return confirmed
                    raise ValidationError("Отметка не требуется; статус изменился")
            prepared = await service.load(target, platform=platform)
            delivered = payload.get("delivery_proof") is True and bool(payload.get("chain_id"))
            if delivered:
                DeliveryChains(self.store, self.api).dispatchable(action)
            if (
                not platform and not delivered and prepared["snapshot"] != payload.get("snapshot")
            ) or prepared["mode"] != payload.get("mode"):
                raise ValidationError("Сделка или требование исполнения изменились")
            values = {key: value for key, value in payload.get("report", {}).items() if key != "state"}
            report = {} if platform else await service.validate(prepared, values, payload.get("attested"))
            if report != payload.get("report"):
                raise ValidationError("Снимок отчёта не соответствует подтверждению")
            if prepared["mode"] == "legacy":
                acknowledgement = await self.mutate(
                    action, "updateDeal", "update_deal", deal_id=target, new_status=ItemDealStatuses.SENT
                )
                if isinstance(acknowledgement, dict):
                    try:
                        confirmed = await service.result(action, observed=acknowledgement)
                    except (ValueError, TypeError):
                        confirmed = None
                    if confirmed:
                        return confirmed
            else:
                await self.mutate(
                    action, "submitFulfillmentReport", "submit_fulfillment", deal_id=target, report=report
                )
            try:
                for attempt in range(10):
                    confirmed = await service.result(action)
                    if confirmed:
                        return confirmed
                    if attempt < 9:
                        await asyncio.sleep(1)
            except APIError as error:
                raise APIError(
                    "FULFILLMENT_OUTCOME_UNKNOWN", uncertain=True, retry_after=error.retry_after
                ) from None
            except (ValueError, TypeError):
                raise APIError("FULFILLMENT_OUTCOME_UNKNOWN", uncertain=True) from None
            raise APIError("FULFILLMENT_NOT_CONFIRMED", uncertain=True)
        if kind == "create":
            draft = self.store.get_draft(target, payload["owner"])
            value = draft["payload"]
            snapshot = await self.products.schema_details(value["category_id"], value["obtaining_type_id"])
            require_conditions(value, snapshot["conditions"])
            form = snapshot["form"]
            validate_draft(value, form, getattr(self.api.account, "role", None))
            attachments = []
            for source in value.get("photos", []):
                path = Path(source).resolve()
                if not path.is_relative_to(Path("data/uploads").resolve()):
                    raise ValidationError("Небезопасный путь вложения")
                attachments.append(path.read_bytes())
            data = [
                SimpleNamespace(id=f["key"], value=value["fields"].get(f["key"], ""))
                for f in form
                if f.get("data_field")
            ]
            options = {
                f["key"]: value["fields"][f["key"]]
                for f in form
                if not f.get("data_field") and not f.get("comment")
            }
            comment = next((value["fields"][f["key"]] for f in form if f.get("comment")), None)
            result = await self.mutate(
                action,
                "createItem",
                "create_item",
                game_category_id=value["category_id"],
                obtaining_type_id=value["obtaining_type_id"],
                name=value["name"],
                price=value["price"],
                description=value["description"],
                options=options,
                data_fields=data,
                attachments=attachments,
                comment=comment,
            )
            if not isinstance(result, dict) or not result.get("id"):
                raise APIError("CREATE_ID_MISSING", uncertain=True)
            expected = {"name": value["name"], "description": value["description"], "price": value["price"]}
            if attachments:
                expected["add_attachments"] = attachments
            if (
                result.get("status") is None
                or result.get("user") is None
                or result.get("category") is None
                or any(field not in result for field in expected if field != "add_attachments")
                or attachments
                and "attachments" not in result
            ):
                created_id = result["id"]
                try:
                    result = await self.owned_item(created_id)
                except (APIError, ValidationError):
                    raise APIError("CREATE_NOT_CONFIRMED", uncertain=True) from None
                if result.get("id") != created_id:
                    raise APIError("CREATE_NOT_CONFIRMED", uncertain=True)
            if (
                result.get("status") != "DRAFT"
                or (result.get("user") or {}).get("id") != self.api.account.id
                or (result.get("category") or {}).get("id") != value["category_id"]
                or not edit_receipt_matches(result["id"], expected, result)
            ):
                raise APIError("CREATE_NOT_CONFIRMED", uncertain=True)
            if value.get("managed_product_id"):
                from app.services.logical_products import LogicalProducts

                self.store.set_setting(
                    "create:candidate:" + action["id"],
                    {
                        "id": result["id"],
                        "account": self.api.account.id,
                        "attachments": [image["id"] for image in result.get("attachments", [])],
                    },
                )
                try:
                    observed = await self.owned_item(result["id"])
                    if observed.get("status") != "DRAFT" or not edit_receipt_matches(
                        result["id"], expected, observed
                    ):
                        raise APIError("MANAGED_CREATE_READBACK_UNKNOWN", uncertain=True)
                    result = observed
                except (APIError, ValueError, TypeError):
                    raise APIError("MANAGED_CREATE_READBACK_UNKNOWN", uncertain=True) from None
            with self.store.transaction() as db:
                if value.get("managed_product_id"):
                    LogicalProducts(self.store, self.api).confirm_creation(
                        db, payload["owner"], target, value, result
                    )
                else:
                    db.execute("INSERT INTO bindings(draft_id,remote_id) VALUES(?,?)", (target, result["id"]))
                    db.execute("UPDATE drafts SET state='REMOTE_DRAFT' WHERE id=?", (target,))
            return self.item_result(result)
        if kind == "discount":
            item = await self.products.owned_item(target)
            fresh = discount_plan(item, payload["price"], getattr(self.api.account, "role", None))
            if fresh != payload:
                raise ValidationError("Условия скидки изменились. Требуется новое подтверждение.")
            result = await self.mutate(action, "updateItem", "update_item", id=target, price=fresh["price"])
            if (
                isinstance(result, dict)
                and result.get("id") == target
                and (result.get("price") is None or result.get("raw_price") is None)
            ):
                try:
                    result = await self.owned_item(target)
                except (APIError, ValidationError):
                    raise APIError("DISCOUNT_RESULT_UNKNOWN", uncertain=True) from None
            if (
                not isinstance(result, dict)
                or result.get("id") != target
                or result.get("price") != fresh["price"]
                or result.get("raw_price") != fresh["expected_raw_price"]
            ):
                raise APIError("DISCOUNT_RESULT_UNKNOWN", uncertain=True)
            return {"remote_id": target, "price": result["price"], "raw_price": result["raw_price"]}
        if kind == "edit_fields":
            draft = self.store.get_draft(payload["draft_id"], payload["owner"])
            value = draft["payload"]
            if value.get("edit_target") != target or draft["state"] != "QUEUED":
                raise ValidationError("Черновик редактирования не соответствует операции")
            item = await self.products.owned_item(target)
            if field_baseline(item) != value.get("edit_baseline"):
                raise ValidationError("Поля товара изменились после подтверждения")
            details = await self.products.schema_details(value["category_id"], value["obtaining_type_id"])
            require_conditions(value, details["conditions"])
            attributes = dict(item.get("attributes") or {})
            fields, comment = [], None
            for field in details["form"]:
                selected = validate_field(field, value["fields"].get(field["key"], ""))
                if field.get("comment"):
                    comment = selected
                elif field.get("data_field"):
                    fields.append(SimpleNamespace(id=field["key"], value=selected))
                else:
                    attributes[field["key"]] = selected
            result = await self.mutate(
                action,
                "updateItem",
                "update_item",
                id=target,
                options=[SimpleNamespace(field=key, value=selected) for key, selected in attributes.items()],
                data_fields=fields,
                comment=comment,
            )
            if not isinstance(result, dict) or result.get("id") not in {None, target}:
                raise APIError("EDIT_NOT_CONFIRMED", uncertain=True)
            observed = await self.owned_item(target)
            if not field_receipt_matches(target, observed, attributes, fields, comment):
                raise APIError("FIELDS_READBACK_NOT_CONFIRMED", uncertain=True)
            return {"remote_id": target}
        if kind == "agreements":
            if payload.get("automatic"):
                from app.services.agreements import AutoAgreements

                return await AutoAgreements(self.store, self.api).execute(self, action)
            if payload.get("account_id") != self.api.account.id:
                raise ValidationError("Аккаунт изменился после подтверждения условий")
            current = await Agreements(self.api).pending(target, payload.get("obtaining_type_id"))
            expected = {entry["id"]: entry for entry in payload["agreements"]}
            if any(entry["id"] not in expected or entry != expected[entry["id"]] for entry in current):
                raise ValidationError("Условия изменились. Прочитайте их и подтвердите заново.")
            accepted = []
            for entry in current:
                try:
                    result = await self.mutate(
                        action,
                        "acceptGameCategoryAgreement",
                        "accept_game_category_agreement",
                        agreement_id=entry["id"],
                    )
                except (ValidationError, Conflict):
                    if accepted:
                        # Earlier agreements in this operation were already accepted by
                        # the server: a local guard failure now is not proof nothing was
                        # sent — keep the external outcome uncertain, never release.
                        raise APIError("AGREEMENT_PARTIAL_STATE", uncertain=True) from None
                    raise
                if not isinstance(result, dict) or result.get("id") != entry["id"]:
                    raise APIError("AGREEMENT_RESULT_UNKNOWN", uncertain=True)
                accepted.append(entry["id"])
            try:
                if await Agreements(self.api).pending(target, payload.get("obtaining_type_id")):
                    raise APIError("AGREEMENT_ACCEPTANCE_NOT_CONFIRMED", uncertain=True)
            except (ValidationError, Conflict) as caught:
                # The final readback happens after server-accepted mutations: a local
                # classification error here must not mark the operation as never-sent.
                raise APIError(getattr(caught, "code", "AGREEMENT_PARTIAL_STATE"), uncertain=True) from None
            return {"accepted_ids": accepted, "confirmed": True}
        if kind == "publish":
            item = await self.owned_item(target)
            if payload.get("native_relist"):
                self.products.native_relist_guard(payload)
                if item.get("status") != "SOLD" or item.get("may_be_published") is not True:
                    raise ValidationError("Playerok больше не разрешает штатное повторное размещение")
                await self.products.prepare_item_agreements(item)
            elif item.get("status") != "DRAFT":
                raise ValidationError("Публикация разрешена только серверному черновику")
            base_price = priority_price_basis(item)
            quote = await self.products.free_priority(target, base_price)
            if (
                quote["id"] != payload["priority_id"]
                or item["price"] != payload["price"]
                or base_price != payload.get("raw_price")
            ):
                raise ValidationError("Условия размещения изменились; требуется новое превью")
            result = await self.mutate(
                action, "publishItem", "publish_item", item_id=target, priority_status_id=quote["id"]
            )
            if isinstance(result, dict) and result.get("id") == target and result.get("status") is None:
                try:
                    result = await self.owned_item(target)
                except (APIError, ValidationError):
                    raise APIError("PUBLICATION_NOT_CONFIRMED", uncertain=True) from None
            if (
                not isinstance(result, dict)
                or result.get("id") != target
                or result.get("status")
                not in {
                    "APPROVED",
                    "PENDING_APPROVAL",
                    "PENDING_MODERATION",
                }
            ):
                raise APIError("PUBLICATION_NOT_CONFIRMED", uncertain=True)
            try:
                current = await self.owned_item(target)
                # Server may legitimately advance the status after the mutation reply
                # (PENDING_APPROVAL→APPROVED); id/owner/price remain strictly checked.
                if current.get("status") not in {"APPROVED", "PENDING_APPROVAL", "PENDING_MODERATION"} or (
                    current.get("price") != item["price"]
                ):
                    raise ValueError
            except (APIError, ValueError, TypeError):
                raise APIError("PUBLICATION_READBACK_UNKNOWN", uncertain=True) from None
            return self.item_result(current)
        if kind in {"edit", "delete"}:
            current = await self.owned_item(target)
            if kind == "delete":
                Products(self.store, self.api).require_deletable(target, action["id"])
                result = await self.mutate(action, "removeItem", "remove_item", id=target)
                if result is not True:
                    raise APIError("DELETE_RESULT_UNKNOWN", uncertain=True)
                return {"removed": True}
            if getattr(self.api.account, "auto_accept_agreements", False):
                await self.products.prepare_item_agreements(current)
            if payload.get("code_migration"):
                from app.services.code_migration import CodeMigration

                CodeMigration(self.store, self.api).guard(action, current)
            if payload.get("logical_binding"):
                from app.services.logical_products import LogicalProducts

                allowed = {"DRAFT", "APPROVED"}
                if payload["logical_binding"].get("platform_binding"):
                    from app.services.platform_orders import PLATFORM_STATUSES

                    allowed = PLATFORM_STATUSES
                    if (
                        current.get("status") not in {"DRAFT", "APPROVED"}
                        and current.get("is_editable") is not True
                    ):
                        raise ValidationError("Playerok не разрешил редактирование архивного товара")
                if current.get("is_editable") is False or current.get("status") not in allowed:
                    raise ValidationError("Текущее состояние Playerok не разрешает изменение описания")
                LogicalProducts(self.store, self.api).binding_guard(action, current)
            changes = {k: v for k, v in payload.items() if k in {"name", "description", "price"}}
            if set(payload) - {
                "name",
                "description",
                "price",
                "add_photos",
                "replace_attachment_ids",
                "keep_attachment_ids",
                "logical_binding",
                "expected_description",
                "expected_name",
                "platform_settings",
                "code_migration",
                "marketing_description",
            }:
                raise ValidationError("Изменение содержит неподдерживаемые поля")
            if payload.get("platform_settings") and not (payload.get("logical_binding") or {}).get(
                "platform_binding"
            ):
                raise ValidationError("Настройки Playerok требуют подтверждаемой привязки")
            if "price" in changes:
                changes["price"] = listing_price(changes["price"], getattr(self.api.account, "role", None))
            for field in ("name", "description"):
                if field in changes:
                    validated = listing_text(changes[field], field)
                    if not payload.get("code_migration"):
                        changes[field] = validated
            protected_description = False
            if marketing := payload.get("marketing_description"):
                from app.services.logical_products import LogicalProducts

                logical = LogicalProducts(self.store, self.api)
                product = logical.listing(target)
                if (
                    not product
                    or product["id"] != marketing
                    or set(payload) != {"description", "expected_description", "marketing_description"}
                    or current.get("description") != payload["expected_description"]
                ):
                    raise ValidationError("Описание или привязка изменились после просмотра")
                logical.verify_marketing_description(product, current, operation_id=action["id"])
            if {"name", "description"} & changes.keys() and not (
                payload.get("logical_binding") or payload.get("code_migration")
            ):
                from app.services.logical_products import LogicalProducts, marker
                from app.services.product_codes import titled

                if (
                    "description" in changes
                    and "expected_description" in payload
                    and current.get("description") != payload["expected_description"]
                ):
                    raise ValidationError("Описание изменилось на Playerok. Откройте редактирование заново")
                if (
                    "name" in changes
                    and "expected_name" in payload
                    and current.get("name") != payload["expected_name"]
                ):
                    raise ValidationError("Название изменилось на Playerok. Откройте редактирование заново")
                product = LogicalProducts(self.store, self.api).listing(target)
                if product:
                    if len(product["code"]) != 3 or marker(current.get("name")) != product["code"]:
                        raise ValidationError(
                            "Код товара не подтверждён. Сначала выполните перенос или сверку"
                        )
                    if "name" in changes:
                        changes["name"] = titled(changes["name"], product["code"])
                    protected_description = True
            keep = []
            if "add_photos" in payload or "keep_attachment_ids" in payload:
                sources = payload.get("add_photos", [])
                if (
                    not isinstance(sources, list)
                    or len(sources) > 10
                    or ("keep_attachment_ids" not in payload and not sources)
                ):
                    raise ValidationError("Требуется от 1 до 10 изображений")
                current_attachments = current.get("attachments") or []
                current_ids = [attachment.get("id") for attachment in current_attachments]
                if any(not ident for ident in current_ids):
                    raise ValidationError("API не вернул идентификаторы фотографий")
                expected = payload.get("replace_attachment_ids")
                if not isinstance(expected, list) or (
                    expected != current_ids
                    if "keep_attachment_ids" in payload
                    else sorted(expected) != sorted(current_ids)
                ):
                    raise ValidationError("Фотографии товара изменились. Повторно откройте превью.")
                keep = payload.get("keep_attachment_ids", [])
                if (
                    not isinstance(keep, list)
                    or any(not isinstance(ident, str) for ident in keep)
                    or len(set(keep)) != len(keep)
                    or not set(keep).issubset(current_ids)
                    or len(keep) + len(sources) > 10
                ):
                    raise ValidationError("Не подтверждён сохраняемый набор фотографий")
                if sources:
                    changes["add_attachments"] = [await Attachments().read(source) for source in sources]
                removed = [ident for ident in current_ids if ident not in keep]
                if removed:
                    changes["remove_attachments"] = removed
            if not changes:
                raise ValidationError("Нет разрешённых изменений")
            if (
                payload.get("logical_binding")
                and not payload.get("code_migration")
                and set(changes) <= {"name", "description"}
                and not {"add_photos", "keep_attachment_ids", "replace_attachment_ids"} & payload.keys()
                and all(current.get(key) == value for key, value in changes.items())
            ):
                # Explicitly confirmed binding of text already on the server:
                # refresh evidence, not an unnecessary updateItem mutation.
                observed = await self.owned_item(target)
                logical = LogicalProducts(self.store, self.api)
                logical.binding_guard(action, observed)
                self.dispatch_guard(action)
                receipt = logical.confirm_binding(action, observed)
                return {**self.item_result(observed), "logical_binding": receipt, "already_present": True}
            from app.services.edit_confirmation import EditConfirmation

            confirmation = EditConfirmation(self.store, self.api)
            proof = confirmation.prepare(action, current, changes, keep)
            try:
                result = await self.mutate(action, "updateItem", "update_item", id=target, **changes)
            except APIError as error:
                if error.uncertain and confirmation.live(action, proof):
                    try:
                        observed = await confirmation.readback(action, proof)
                        # A lost response has no causal receipt. Record only the
                        # observed postcondition, never invent mutation success.
                        confirmation.record(action, proof, observed, None, confirmed=False)
                    except (APIError, Conflict):
                        pass  # Preserve the original uncertain mutation outcome.
                raise
            observed = None
            if not edit_receipt_matches(target, proof["expected"], result, keep_attachment_ids=keep):
                observed = await confirmation.readback(action, proof)
                result = observed
            if protected_description or "keep_attachment_ids" in payload:
                try:
                    observed = observed or await confirmation.readback(action, proof)
                    if "keep_attachment_ids" in payload and {
                        a["id"] for a in observed.get("attachments", [])
                    } != {a["id"] for a in result.get("attachments", [])}:
                        raise APIError("EDIT_READBACK_UNKNOWN", uncertain=True)
                except APIError:
                    raise APIError("EDIT_READBACK_UNKNOWN", uncertain=True) from None
            if payload.get("code_migration"):
                from app.services.code_migration import CodeMigration

                try:
                    observed = await self.owned_item(target)
                    receipt = CodeMigration(self.store, self.api).confirm(action, observed)
                except (APIError, ValueError, TypeError):
                    raise APIError("CODE_MIGRATION_READBACK_UNKNOWN", uncertain=True) from None
                return {**self.item_result(observed), "code_migration": receipt}
            if payload.get("logical_binding"):
                try:
                    observed = await self.owned_item(target)
                    receipt = LogicalProducts(self.store, self.api).confirm_binding(action, observed)
                except (APIError, ValueError, TypeError):
                    raise APIError("LOGICAL_BINDING_READBACK_UNKNOWN", uncertain=True) from None
                return {**self.item_result(observed), "logical_binding": receipt}
            if protected_description and "name" in changes:
                from app.services.logical_products import critical, marked_title

                try:
                    before_identity, after_identity = critical(current), critical(observed)
                    # Price has its own requested/unchanged readback comparison.
                    before_identity.pop("price")
                    after_identity.pop("price")
                    if after_identity != before_identity:
                        raise ValueError
                    title = marked_title(observed["name"], product["code"])
                except ValueError:
                    raise APIError("TITLE_EDIT_READBACK_UNKNOWN", uncertain=True) from None
                receipt = {
                    **self.item_result(observed),
                    "identity_title": {
                        "product_id": product["id"],
                        "item_id": target,
                        "code": product["code"],
                        "revision": product["revision"],
                        "control_revision": product["control_revision"],
                        "prior_title": product["title_snapshot"],
                        "title": title,
                    },
                }
                confirmation.record(action, proof, observed, receipt)
                return receipt
            receipt = self.item_result(result)
            if marketing:
                from app.services.logical_products import critical, digest

                try:
                    if critical(observed) != critical(current):
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise APIError("EDIT_READBACK_UNKNOWN", uncertain=True) from None
                receipt["marketing_description"] = {
                    "prior": product["description_sha256"],
                    "current": digest(observed["description"]),
                    "revision": product["revision"],
                    "gift": product["handler"] == "telegram_gifts",
                }
            confirmation.record(action, proof, observed if observed is not None else result, receipt)
            return receipt
        if kind == "mark_read":
            await self.api.call("get_chat", chat_id=target)
            result = await self.mutate(action, "markChatAsRead", "mark_chat_as_read", chat_id=target)
            if result.get("id") != target or result.get("unread_messages_counter") != 0:
                raise APIError("READ_STATUS_NOT_CONFIRMED", uncertain=True)
            return {"chat_id": target, "unread_messages_counter": 0}
        if kind == "reply":
            if payload.get("paid_greeting"):
                from app.services.paid_greeting import prepare

                payload = {"text": await prepare(self.store, self.api, action)}
                if payload["text"] is None:
                    return {"recipient_request_not_needed": True}
            elif payload.get("review_thanks"):
                from app.services.review_thanks import verify

                await verify(self.store, self.api, action)
            elif payload.get("auto_reply"):
                await AutoReplies(self.store, self.api).verify(action)
            else:
                await self.api.call("get_chat", chat_id=target)
            try:
                payload = reply_payload(payload)
            except ValueError as error:
                raise ValidationError(str(error)) from None
            sources = payload.get("photos", [])
            # Validate/read every local file before the first external upload.
            images = [await Attachments().read(source) for source in sources]
            image_ids = []
            for index, image in enumerate(images):
                client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"playerok:{action['id']}:{index}"))
                self.store.execute(
                    "INSERT INTO chat_uploads(operation_id,position,client_attachment_id,status) VALUES(?,?,?,'PROCESSING')",
                    (action["id"], index, client_id),
                )
                uploaded = await self.mutate(
                    action,
                    "uploadChatImageIntoTemporaryStore",
                    "upload_chat_image",
                    image=image,
                    chat_id=target,
                    client_attachment_id=client_id,
                )
                if (
                    not isinstance(uploaded, dict)
                    or not uploaded.get("id")
                    or uploaded.get("chatId") != target
                    or uploaded.get("clientAttachmentId") != client_id
                ):
                    raise APIError("CHAT_UPLOAD_NOT_CONFIRMED", uncertain=True)
                try:
                    expiry = datetime.fromisoformat(uploaded["expiresAt"])
                    if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                        raise ValueError
                except (KeyError, ValueError, TypeError, AttributeError):
                    raise APIError("CHAT_UPLOAD_EXPIRY_UNKNOWN", uncertain=True) from None
                self.store.execute(
                    "UPDATE chat_uploads SET status='UPLOADED',attachment_id=?,expires_at=? WHERE operation_id=? AND position=?",
                    (uploaded["id"], uploaded["expiresAt"], action["id"], index),
                )
                image_ids.append(uploaded["id"])
            if image_ids:
                uploads = self.store.rows(
                    "SELECT expires_at FROM chat_uploads WHERE operation_id=?", (action["id"],)
                )
                if any(
                    datetime.fromisoformat(upload["expires_at"]) <= datetime.now(UTC) for upload in uploads
                ):
                    # Uploads already exist on the server: the message was never sent,
                    # but the operation is not "nothing happened" — requeueing would
                    # re-upload; keep it uncertain for manual review instead.
                    raise APIError("CHAT_UPLOAD_EXPIRED_AFTER_UPLOAD", uncertain=True)
                result = await self.mutate(
                    action,
                    "createChatMessage",
                    "send_message_with_images",
                    chat_id=target,
                    text=payload["text"],
                    image_ids=image_ids,
                )
                returned = result.get("images") if isinstance(result, dict) else None
                if (
                    not isinstance(returned, list)
                    or len(returned) != len(image_ids)
                    or any(not isinstance(image, dict) or not image.get("id") for image in returned)
                ):
                    raise APIError("MESSAGE_IMAGES_NOT_CONFIRMED", uncertain=True)
                if not result.get("id"):
                    raise APIError("MESSAGE_ID_MISSING", uncertain=True)
                return {"message_id": result["id"], "image_count": len(image_ids)}
            result = await self.mutate(
                action, "createChatMessage", "send_message", chat_id=target, text=payload["text"]
            )
            if not result.get("id"):
                raise APIError("MESSAGE_ID_MISSING", uncertain=True)
            return {
                "message_id": result["id"],
                **(
                    {"recipient_requested": action["recipient_requested"]}
                    if "recipient_requested" in action
                    else {}
                ),
            }
        if kind == "delivery":
            from app.services.logical_products import LogicalProducts, stock_scope

            local_item = self.store.rows("SELECT item_id FROM orders WHERE id=?", (target,))
            if local_item:
                await LogicalProducts(self.store, self.api).fresh_delivery_item(
                    local_item[0]["item_id"], target
                )

            from app.services.order_management import deal_details, delivery_chat_allowed

            order = await deal_details(self.api, target)
            deliverable(order, target, self.api.account.id, wait_for_details=bool(payload.get("chain_id")))
            Orders(self.store).ingest(order)
            chat = (order.get("chat") or {}).get("id")
            current_chat = await self.api.call("get_chat", chat_id=chat)
            if not delivery_chat_allowed(current_chat, chat, wait_for_details=bool(payload.get("chain_id"))):
                raise ValidationError("Чат недоступен для выдачи")
            stock = self.store.rows(
                "SELECT i.secret,i.status,i.item_id FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id=?",
                (target,),
            )
            if (
                not chat
                or not stock
                or stock[0]["status"] != "RESERVED"
                or stock[0]["item_id"] != stock_scope(self.store, (order.get("item") or {}).get("id"), target)
            ):
                raise ValidationError("Резерв не соответствует сделке")
            result = await self.mutate(
                action, "createChatMessage", "send_message", chat_id=chat, text=stock[0]["secret"]
            )
            if not result.get("id"):
                raise APIError("DELIVERY_RESULT_UNKNOWN", uncertain=True)
            return {"message_id": result["id"]}
        raise ValidationError("Неподдерживаемый тип операции")


class OutboxWorker:
    def __init__(self, store, executor):
        self.store, self.executor = store, executor

    def finish(self, ident, token, status, result=None, error=None, retry_after=None):
        try:
            self.store.finish(ident, token, status, result=result, error=error, retry_after=retry_after)
        except Conflict:
            self.store.record_late_result(ident, token, status, result=result, error=error)

    async def once(self, *, kinds=None, excluded_kinds=(), operation_id=None):
        # Defense in depth: read-only mode never claims any other pending action,
        # even if the caller forgot to pass the agreement-only filter.
        account = getattr(getattr(self.executor, "api", None), "account", None)
        if getattr(account, "auto_accept_agreements", False) and not account.writes_enabled:
            kinds = ("agreements",) if kinds is None else tuple(k for k in kinds if k == "agreements")
        recovered = self.store.recover()
        if recovered:
            event(
                "WARNING",
                f"Восстановлено операций: {recovered}. Неизвестные отправки не повторяются.",
                level=30,
            )
        action = self.store.claim(
            lease_seconds=180, kinds=kinds, excluded_kinds=excluded_kinds, operation_id=operation_id
        )
        if not action:
            return False
        order_target = (
            action["target"]
            if action["kind"]
            in {
                "delivery",
                "delivery_text",
                "chain_message",
                "fulfill",
                "gift_send",
                "gift_notice",
                "gift_refund",
            }
            else None
        )
        if action["kind"] == "reply":
            matches = self.store.rows("SELECT id FROM orders WHERE chat_id=? LIMIT 2", (action["target"],))
            order_target = matches[0]["id"] if len(matches) == 1 else None
        event(
            "CHECK",
            "Начало операции: " + action["kind"],
            order=action["target"],
            operation=action["id"],
            level=10,
        )
        try:
            result = await self.executor(action)
        except (ValidationError, Conflict):
            # Local guards (launch stop, stale confirmation, changed readiness) prove the
            # request was never sent: PERMANENT_FAILED, not an uncertain external outcome.
            self.finish(action["id"], action["lease_token"], "PERMANENT_FAILED", error="VALIDATION_FAILED")
        except APIError as error:
            from app.services.operation_outcomes import unstarted_delivery, unstarted_listing

            with self.store.transaction() as db:
                delivery_retry = unstarted_delivery(db, action)
                listing_retry = unstarted_listing(db, action)
            if (
                error.code == "WRITES_DISABLED"
                and (delivery_retry or listing_retry)
                and getattr(
                    getattr(getattr(self.executor, "api", None), "connection", None),
                    "awaiting_verification",
                    False,
                )
            ):
                # SafeAccount.permit can lose the race to the profile probe
                # before dispatch_guard runs. Positive journal proof is required.
                error = APIError("CONNECTION_NOT_VERIFIED")
            gift_retry = False
            visibility_retry = False
            from app.services.paid_greeting import unstarted_recipient_request

            with self.store.transaction() as db:
                request_retry = unstarted_recipient_request(db, action["id"])
            if action["kind"] in {"stock_pause", "stock_resume"}:
                from app.services.stock_visibility import unstarted

                with self.store.transaction() as db:
                    visibility_retry = unstarted(db, action)
            if action["kind"] == "gift_send":
                from app.gifts.outcomes import unstarted_preflight

                with self.store.transaction() as db:
                    gift_retry = unstarted_preflight(db, action)
            self.finish(
                action["id"],
                action["lease_token"],
                "RETRYABLE_FAILED"
                if (
                    action["kind"] in {"delivery", "delivery_text", "chain_message", "reply", "mark_read"}
                    and action["attempts"] < 4
                    or gift_retry
                    or delivery_retry
                    or visibility_retry
                    or listing_retry
                    or request_retry
                )
                and not error.uncertain
                and (
                    error.code
                    in {
                        "NETWORK_ERROR",
                        "HTTP_503",
                        "HTTP_502",
                        "HTTP_504",
                        "HTTP_500",
                        "HTTP_429",
                        "RATE_LIMITED",
                        "CONNECTION_NOT_VERIFIED",
                        "SESSION_EXPIRED",
                        "UNAUTHORIZED_OR_CHALLENGE",
                        "GIFT_PREFLIGHT_NETWORK",
                        "MARKETING_EDIT_READBACK_PENDING",
                    }
                    or delivery_retry
                    and error.code
                    in {
                        "DELIVERY_CHAT_NOT_YET_AVAILABLE",
                        "DELIVERY_SNAPSHOT_NOT_YET_AVAILABLE",
                        "DELIVERY_LISTING_DETAILS_NOT_YET_AVAILABLE",
                        "DELIVERY_SELLER_NOT_YET_AVAILABLE",
                        "DELIVERY_FLAGS_NOT_YET_AVAILABLE",
                        "PLATFORM_DETAILS_NOT_YET_AVAILABLE",
                    }
                    or gift_retry
                    and error.code
                    in {
                        "PROXY_UNAVAILABLE",
                        "PROXY_ROUTE_CHANGED",
                        "MTPROTO_TIMEOUT",
                        "GIFT_TEMPORARY_PAUSE",
                        "GIFT_SYNC_PENDING",
                        "RECIPIENT_DATA_PENDING",
                        "PAYMENT_LOCKED",
                        "GIFT_UNAVAILABLE",
                        "GIFT_QUANTITY_UNAVAILABLE",
                        "BALANCE_TOO_LOW",
                        "BALANCE_UNAVAILABLE",
                        "CATALOG_UNAVAILABLE",
                        "BALANCE_OBSERVATION_STALE",
                        "DAILY_BUDGET_EXHAUSTED",
                        "GIFT_LISTING_DETAILS_NOT_YET_AVAILABLE",
                        "DELIVERY_SNAPSHOT_NOT_YET_AVAILABLE",
                        "DELIVERY_FLAGS_NOT_YET_AVAILABLE",
                    }
                    or visibility_retry
                    and error.code
                    in {
                        "VISIBILITY_PREPARE_CHANGED",
                        "VISIBILITY_DETAILS_INCOMPLETE",
                        "VISIBILITY_NOT_AVAILABLE",
                        "VISIBILITY_BINDING_PENDING",
                    }
                )
                else "UNCERTAIN"
                if error.uncertain
                else "PERMANENT_FAILED",
                error=error.code,
                retry_after=error.retry_after,
            )
        except asyncio.CancelledError:
            self.finish(action["id"], action["lease_token"], "UNCERTAIN", error="PROCESS_CANCELLED")
            raise
        except Exception:  # noqa: BLE001 -- terminal safety boundary, records UNCERTAIN, never retries
            self.finish(action["id"], action["lease_token"], "UNCERTAIN", error="UNEXPECTED_FAILURE")
        else:
            self.finish(action["id"], action["lease_token"], "SUCCEEDED", result=result)
        self.store.audit(
            None,
            action["kind"],
            action["target"],
            self.store.rows("SELECT status FROM outbox WHERE id=?", (action["id"],))[0]["status"],
        )
        outcome = self.store.rows("SELECT status,error_code FROM outbox WHERE id=?", (action["id"],))[0]
        if action["kind"] == "reply" and action["payload"].get("review_thanks"):
            event(
                "CHECK",
                f"Review reply outcome: {outcome['status']} · {outcome['error_code']}",
                operation=action["id"],
                level=10,
            )
            return True
        from app.services.fulfillment import automatic_legacy_completion

        checking_completion = (
            action["kind"] == "fulfill"
            and outcome["status"] == "UNCERTAIN"
            and automatic_legacy_completion(action["payload"], action["target"])
        )
        if outcome["status"] != "SUCCEEDED":
            from app.gifts.core import gift_reason

            waiting_recipient = (
                action["kind"] == "gift_send"
                and outcome["status"] == "RETRYABLE_FAILED"
                and outcome["error_code"] == "RECIPIENT_DATA_PENDING"
            )
            repeated_wait = outcome["status"] == "RETRYABLE_FAILED" and outcome["error_code"] == action.get(
                "error_code"
            )
            intent = self.store.setting("promotions:intent:" + action["id"], {})
            checking_receipt = (
                action["kind"] == "premium_bump"
                and action["payload"].get("automatic")
                and outcome["status"] == "UNCERTAIN"
                and intent.get("automatic_receipt_check")
                and (intent.get("mutation_returned") or "candidate_id" in intent)
            )
            if checking_completion:
                message = "Проверяем отметку «Выполнено» на Playerok"
            elif outcome["status"] == "UNCERTAIN":
                message = (
                    "⏳ Проверяем результат поднятия — "
                    + str(action.get("display_name") or action["target"])[:150]
                    if checking_receipt
                    else "⛔ Результат операции неизвестен. Повтор запрещён до сверки."
                )
            elif action["kind"] == "stock_pause" and outcome["error_code"] == "VISIBILITY_NOT_AVAILABLE":
                message = "📦 Пустой склад: Playerok не разрешил снять лот с продажи. Проверка продолжится автоматически."
            elif action["kind"].startswith("gift_"):
                message = gift_reason(outcome["error_code"])
                if outcome["status"] == "RETRYABLE_FAILED" and not waiting_recipient:
                    message += " Проверка продолжится автоматически."
            elif outcome["status"] == "RETRYABLE_FAILED":
                message = (
                    "⏳ Заказ ожидает подтверждённых данных; безопасная проверка продолжится автоматически."
                )
            else:
                message = "⛔ Действие не выполнено. Причина сохранена в истории операции."
            event(
                "CHECK",
                "Outbox outcome " + str(outcome),
                order=action["target"],
                operation=action["id"],
                level=10,
            )
            if order_target:
                order = self.store.rows("SELECT payload FROM orders WHERE id=?", (order_target,))
                if order:
                    from app.services.shop_events import buyer, order_title

                    value = json.loads(order[0]["payload"])
                    message = f"{message} · «{order_title(value)}» · {buyer(value)}"
            event(
                "INFO" if checking_receipt or checking_completion or waiting_recipient else "WARNING",
                message,
                order=order_target,
                operation=action["id"],
                level=10
                if repeated_wait or checking_receipt
                else 20
                if checking_completion or waiting_recipient
                else 30,
            )
        elif action["kind"] in {"publish", "premium_publish"} and result.get("status") == "APPROVED":
            from app.services.shop_events import product_title

            event(
                "✅ ТОВАР",
                f"📦 Товар «{product_title(result.get('name'))}» опубликован",
                operation=action["id"],
                success=True,
            )
        elif action["kind"] in {"stock_pause", "stock_resume"} and (
            result.get("bot_paused") or action["kind"] == "stock_resume" and not result.get("already_active")
        ):
            event(
                "SUCCESS",
                "🔄 Лот автоматически снят с продажи"
                if action["kind"] == "stock_pause"
                else "🔄 Лот возвращён в продажу",
                operation=action["id"],
            )
        elif (
            action["kind"] == "premium_bump"
            and result.get("payment_id")
            and result.get("paid_rub") is not None
        ):
            event(
                "SUCCESS",
                f"🚀 Поднял лот — {str(result.get('name') or 'Товар')[:110]} — списано {result['paid_rub']} ₽",
                operation=action["id"],
            )
        return True
