import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.playerok.adapter import APIError, plain
from app.services.products import ValidationError, validate_draft


class Executor:
    def __init__(self, store, api, products):
        self.store, self.api, self.products = store, api, products

    async def mutate(self, operation, method, **kwargs):
        async with self.api.lock:

            def run():
                with self.api.account.permit(operation):
                    try:
                        return plain(getattr(self.api.account, method)(**kwargs))
                    except APIError:
                        raise
                    except Exception:  # noqa: BLE001 -- mutation may have succeeded; preserve uncertainty
                        # Parsing may fail after the mutation already reached the server.
                        raise APIError("MUTATION_RESULT_UNKNOWN", uncertain=True) from None

            return await asyncio.to_thread(run)

    async def owned_item(self, item_id):
        item = await self.api.call("get_item", id=item_id)
        if not self.api.account.id or (item.get("user") or {}).get("id") != self.api.account.id:
            raise ValidationError("Владение лотом не подтверждено")
        return item

    async def __call__(self, action):
        kind, target, payload = action["kind"], action["target"], action["payload"]
        if kind == "create":
            draft = self.store.get_draft(target, payload["owner"])
            value = draft["payload"]
            form = await self.products.schema(value["category_id"], value["obtaining_type_id"])
            validate_draft(value, form)
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
            options = {f["key"]: value["fields"][f["key"]] for f in form if not f.get("data_field")}
            result = await self.mutate(
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
            )
            if not result.get("id"):
                raise APIError("CREATE_ID_MISSING", uncertain=True)
            with self.store.transaction() as db:
                db.execute("INSERT INTO bindings(draft_id,remote_id) VALUES(?,?)", (target, result["id"]))
                db.execute("UPDATE drafts SET state='REMOTE_DRAFT' WHERE id=?", (target,))
            return {"remote_id": result["id"]}
        if kind == "publish":
            item = await self.owned_item(target)
            if item.get("status") != "DRAFT":
                raise ValidationError("Публикация разрешена только серверному черновику")
            quote = await self.products.free_priority(target, item["price"])
            if quote["id"] != payload["priority_id"] or item["price"] != payload["price"]:
                raise ValidationError("Условия размещения изменились; требуется новое превью")
            result = await self.mutate(
                "publishItem", "publish_item", item_id=target, priority_status_id=quote["id"]
            )
            if result.get("id") != target or result.get("status") == "DRAFT":
                raise APIError("PUBLICATION_NOT_CONFIRMED", uncertain=True)
            return {"remote_id": target, "status": result.get("status")}
        if kind in {"edit", "delete"}:
            await self.owned_item(target)
            if kind == "delete":
                result = await self.mutate("removeItem", "remove_item", id=target)
                return {"removed": bool(result)}
            changes = {k: v for k, v in payload.items() if k in {"name", "description", "price"}}
            if not changes:
                raise ValidationError("Нет разрешённых изменений")
            result = await self.mutate("updateItem", "update_item", id=target, **changes)
            if result.get("id") != target:
                raise APIError("EDIT_NOT_CONFIRMED", uncertain=True)
            return {"remote_id": target}
        if kind == "reply":
            await self.api.call("get_chat", chat_id=target)
            result = await self.mutate(
                "createChatMessage", "send_message", chat_id=target, text=payload["text"]
            )
            if not result.get("id"):
                raise APIError("MESSAGE_ID_MISSING", uncertain=True)
            return {"message_id": result["id"]}
        if kind == "delivery":
            order = await self.api.call("get_deal", id=target)
            if order.get("status") != "PAID" or order.get("direction") != "OUT":
                raise ValidationError("Актуальная сделка не является оплаченной продажей")
            chat = (order.get("chat") or {}).get("id")
            stock = self.store.rows(
                "SELECT i.secret,i.status,i.item_id FROM inventory i JOIN reservations r ON r.inventory_id=i.id WHERE r.order_id=?",
                (target,),
            )
            if (
                not chat
                or not stock
                or stock[0]["status"] != "RESERVED"
                or stock[0]["item_id"] != (order.get("item") or {}).get("id")
            ):
                raise ValidationError("Резерв не соответствует сделке")
            result = await self.mutate(
                "createChatMessage", "send_message", chat_id=chat, text=stock[0]["secret"]
            )
            if not result.get("id"):
                raise APIError("DELIVERY_RESULT_UNKNOWN", uncertain=True)
            return {"message_id": result["id"]}
        raise ValidationError("Неподдерживаемый тип операции")


class OutboxWorker:
    def __init__(self, store, executor):
        self.store, self.executor = store, executor

    async def once(self):
        self.store.recover()
        action = self.store.claim(lease_seconds=180)
        if not action:
            return False
        try:
            result = await self.executor(action)
        except ValidationError:
            self.store.finish(
                action["id"], action["lease_token"], "PERMANENT_FAILED", error="VALIDATION_FAILED"
            )
        except APIError as error:
            self.store.finish(
                action["id"],
                action["lease_token"],
                "UNCERTAIN" if error.uncertain else "PERMANENT_FAILED",
                error=error.code,
            )
        except asyncio.CancelledError:
            self.store.finish(action["id"], action["lease_token"], "UNCERTAIN", error="PROCESS_CANCELLED")
            raise
        except Exception:  # noqa: BLE001 -- terminal safety boundary, records UNCERTAIN, never retries
            self.store.finish(action["id"], action["lease_token"], "UNCERTAIN", error="UNEXPECTED_FAILURE")
        else:
            self.store.finish(action["id"], action["lease_token"], "SUCCEEDED", result=result)
        self.store.audit(
            None,
            action["kind"],
            action["target"],
            self.store.rows("SELECT status FROM outbox WHERE id=?", (action["id"],))[0]["status"],
        )
        return True
