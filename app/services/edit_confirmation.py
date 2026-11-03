"""Read-only edit confirmation. This service never invokes a mutation."""

import asyncio
import hashlib
import time

from app.database.store import Conflict, encode, now
from app.playerok.adapter import APIError
from app.services.item_receipts import edit_receipt_matches


def payload_hash(payload):
    return hashlib.sha256(encode(payload).encode()).hexdigest()


class EditConfirmation:
    def __init__(self, store, api):
        self.store, self.api = store, api

    @staticmethod
    def key(ident):
        return "edit:confirmation:" + ident

    def prepare(self, action, current, changes, keep):
        expected = {key: value for key, value in changes.items() if key != "add_attachments"}
        if "add_attachments" in changes or "keep_attachment_ids" in action["payload"]:
            expected["add_attachments"] = [True] * len(changes.get("add_attachments", []))
        proof = {
            "payload_hash": payload_hash(action["payload"]),
            "account": self.api.account.id,
            "expected": expected,
            "keep": keep,
            "before": current,
            "lease_token": action["lease_token"],
            "prepared_at": now(),
        }
        self.store.set_setting(self.key(action["id"]), proof)
        return proof

    def matches(self, target, proof, observed):
        if not edit_receipt_matches(target, proof["expected"], observed, keep_attachment_ids=proof["keep"]):
            return False
        if (observed.get("user") or {}).get("id") != proof["account"]:
            return False
        # Missing evidence is never filled with the old snapshot. In particular,
        # a successful edit cannot attest an externally changed delivery contract.
        before = proof["before"]
        fields = {"category", "obtaining_type", "attributes", "data_fields", "comment"}
        fields |= {"name", "description", "price"} - proof["expected"].keys()
        return all(
            before.get(field) is None
            or (
                edit_receipt_matches(target, {"price": before[field]}, observed)
                if field == "price"
                else observed.get(field) == before[field]
            )
            for field in fields
        )

    async def readback(self, action, proof):
        for delay in (0, 0.1, 0.3):
            if delay:
                await asyncio.sleep(delay)  # Bounded visibility backoff; never a write retry.
            try:
                observed = await self.api.call("get_item", id=action["target"])
            except APIError:
                continue
            if self.api.account.id != proof["account"]:
                break
            if self.matches(action["target"], proof, observed):
                return observed
        raise APIError("EDIT_NOT_CONFIRMED", uncertain=True)

    def record(self, action, proof, observed, result):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT status,lease_token,lease_until FROM outbox WHERE id=?", (action["id"],)
            ).fetchone()
            if (
                not row
                or row["status"] != "PROCESSING"
                or row["lease_token"] != action["lease_token"]
                or (row["lease_until"] or 0) <= time.time()
                or self.api.account.id != proof["account"]
            ):
                raise Conflict("Lease проверки редактирования потерян")
            self.store.set_setting(
                self.key(action["id"]),
                {**proof, "confirmed": True, "observed": observed, "result": result, "checked_at": now()},
            )

    async def evidence(self, action):
        proof = self.store.setting(self.key(action["id"]), {})
        observed = await self.api.call("get_item", id=action["target"])
        if (
            not self.api.account.id
            or (observed.get("user") or {}).get("id") != self.api.account.id
            or observed.get("id") != action["target"]
        ):
            raise Conflict("Актуальный ID или владелец объявления не подтверждён")
        if not proof.get("confirmed") or proof.get("payload_hash") != payload_hash(action["payload"]):
            payload = action["payload"]
            expected = {
                key: value for key, value in payload.items() if key in {"name", "description", "price"}
            }
            from app.services.logical_products import LogicalProducts, marked_title, marker
            from app.services.products import listing_text

            for field in ("name", "description"):
                if field in expected:
                    try:
                        expected[field] = listing_text(expected[field], field)
                    except ValueError:
                        pass  # Historical payload is evidence, not a new form submission.
            binding = LogicalProducts(self.store, self.api).listing(action["target"])
            if (
                "name" in expected
                and binding
                and len(binding["code"]) == 3
                and marker(observed.get("name")) == binding["code"]
            ):
                expected["name"] = marked_title(expected["name"], binding["code"])
            if "add_photos" in payload or "keep_attachment_ids" in payload:
                expected.update(
                    add_attachments=[True] * len(payload.get("add_photos", [])),
                    remove_attachments=[
                        i
                        for i in payload.get("replace_attachment_ids", [])
                        if i not in payload.get("keep_attachment_ids", [])
                    ],
                )
            matches = bool(expected) and edit_receipt_matches(
                action["target"],
                expected,
                observed,
                keep_attachment_ids=payload.get("keep_attachment_ids", []),
            )
            return {
                "decision": "CURRENT_STATE_ONLY",
                "matches": matches,
                "remote_id": action["target"],
                "checked_at": now(),
                "reason": "Поля сейчас совпадают, но нет сохранённого подтверждения исходной записи. Playerok не предоставляет здесь ревизию изменения; результат старой операции остаётся неизвестным."
                if matches
                else "Playerok не подтвердил все запрошенные изменения. Исходная запись могла выполниться, а затем измениться; повтор запрещён.",
            }
        if self.api.account.id != proof["account"]:
            raise Conflict("Аккаунт операции не подтверждён")
        # A later local mutation invalidates attribution even if it restored the
        # same values. Playerok exposes no usable edit revision in this contract.
        if self.store.rows(
            "SELECT 1 FROM outbox WHERE target=? AND id<>? AND started_at IS NOT NULL AND started_at>=? LIMIT 1",
            (action["target"], action["id"], action.get("started_at") or action["created_at"]),
        ):
            raise Conflict("После этой операции начиналось другое изменение. Требуется ручная проверка")
        if not self.matches(action["target"], proof, observed):
            raise Conflict("Текущее состояние не совпадает с сохранённым подтверждением редактирования")
        if "attachments" in proof["observed"] and sorted(
            a.get("id", "") for a in observed.get("attachments") or []
        ) != sorted(a.get("id", "") for a in proof["observed"]["attachments"] or []):
            raise Conflict("Фотографии изменились после подтверждения")
        return {"decision": "CONFIRM_READ_STATE", **proof["result"], "checked_at": now()}


def finish_edit(db, action, result):
    """Shared atomic effects after verified success, including owner recovery."""
    payload = action["payload"]
    if "description" in payload and not (payload.get("logical_binding") or payload.get("code_migration")):
        db.execute("UPDATE logical_listings SET verified_at=NULL WHERE item_id=?", (action["target"],))
    if result and result.get("identity_title"):
        title = result["identity_title"]
        db.execute(
            "UPDATE logical_listings SET title_snapshot=? WHERE item_id=? AND product_id=? AND title_snapshot IS ? AND EXISTS(SELECT 1 FROM logical_products p WHERE p.id=logical_listings.product_id AND p.code=? AND p.revision=? AND p.control_revision=?)",
            (
                title["title"],
                title["item_id"],
                title["product_id"],
                title["prior_title"],
                title["code"],
                title["revision"],
                title["control_revision"],
            ),
        )
    if payload.get("logical_binding"):
        from app.services.logical_products import finish_binding

        finish_binding(db, action, "SUCCEEDED", result)
    if payload.get("code_migration"):
        from app.services.code_migration import finish_migration

        finish_migration(db, action, "SUCCEEDED", result)
