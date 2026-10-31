import hashlib
import json

from app.services.products_errors import ValidationError


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def agreement_record(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not value["id"]
        or not isinstance(value.get("description"), str)
        or not value["description"].strip()
    ):
        raise ValidationError("Playerok вернул неполное соглашение")
    if value.get("__typename", "GameCategoryAgreement") != "GameCategoryAgreement" or value.get(
        "type"
    ) not in {None, "SELLER"}:
        raise ValidationError("Неподдерживаемый тип соглашения; автопринятие запрещено")
    return {
        "id": value["id"],
        "description": value["description"],
        "game_category_id": value.get("game_category_id"),
        "game_category_obtaining_type_id": value.get("game_category_obtaining_type_id"),
    }


class Agreements:
    def __init__(self, api):
        self.api = api

    async def pending(self, category_id, obtaining_type_id):
        values = {}
        async for page in self.api.pages(
            "get_game_category_agreements", "agreements", game_category_id=category_id
        ):
            for value in page:
                record = agreement_record(value)
                if record["game_category_id"] not in {None, category_id}:
                    raise ValidationError("Соглашение не относится к выбранной категории")
                if record["game_category_obtaining_type_id"] not in {None, obtaining_type_id}:
                    continue
                if record["id"] in values and values[record["id"]] != record:
                    raise ValidationError("Противоречивые версии соглашения Playerok")
                values[record["id"]] = record
        return sorted(values.values(), key=lambda value: value["id"])

    async def snapshot(self, category, obtaining):
        pending = await self.pending(category["id"], obtaining.get("id"))
        documents = {}
        for scope, entry in [("category", category), ("obtaining", obtaining)]:
            instruction = entry.get("instruction_for_seller")
            if instruction:
                if not isinstance(instruction, str):
                    raise ValidationError("Неполная инструкция продавцу")
                documents[f"{scope}:instruction"] = {
                    "id": f"{scope}:instruction",
                    "title": "Инструкция продавцу",
                    "text": instruction,
                }
            for agreement in entry.get("agreements") or []:
                record = agreement_record(agreement)
                if record["game_category_id"] not in {None, category["id"]}:
                    raise ValidationError("Соглашение не относится к выбранной категории")
                if record["game_category_obtaining_type_id"] not in {None, obtaining.get("id")}:
                    continue
                document = {
                    "id": record["id"],
                    "title": "Условия Playerok",
                    "text": record["description"],
                }
                if record["id"] in documents and documents[record["id"]] != document:
                    raise ValidationError("Противоречивые версии соглашения Playerok")
                documents[record["id"]] = document
        for record in pending:
            document = {"id": record["id"], "title": "Условия Playerok", "text": record["description"]}
            if record["id"] in documents and documents[record["id"]] != document:
                raise ValidationError("Условия Playerok изменились во время загрузки")
            documents[record["id"]] = document
        documents = sorted(documents.values(), key=lambda value: value["id"])
        return {"documents": documents, "hash": fingerprint(documents), "pending": pending}


def require_conditions(payload, conditions):
    if conditions["pending"]:
        if conditions.get("automatic"):
            raise ValidationError(
                "Принятие условий ещё не подтверждено: "
                + conditions.get("auto_status", "WAITING")
                + ". Доступна безопасная проверка без повторной отправки."
            )
        raise ValidationError("На Playerok есть непринятые условия категории. Откройте «Условия» в превью.")
    if conditions.get("auto_confirmed"):
        return
    if conditions["documents"] and payload.get("conditions_ack") != conditions["hash"]:
        raise ValidationError("Условия или инструкция изменились. Прочитайте их повторно в превью.")


class AutoAgreements:
    """Narrow owner policy; only durable agreement operations, never other writes."""

    def __init__(self, store, api):
        self.store, self.api = store, api

    def authority(self, expected=None):
        a = self.api.account
        if not getattr(a, "auto_accept_agreements", False):
            raise ValidationError("Автопринятие соглашений выключено")
        if (
            not a.id
            or (expected is not None and expected != a.id)
            or getattr(a, "connection_verified", False) is not True
            or getattr(a, "verified_account_id", None) != a.id
            or self.store.setting("playerok:bound_account_id") != a.id
        ):
            raise ValidationError("Привязка Playerok не подтверждена; принятие условий заблокировано")
        return a.id

    def journal(self, category, obtaining, snapshot, status, operation=None):
        from app.database.store import now

        account = self.authority()
        value = {
            "account_id": account,
            "category_id": category,
            "obtaining_type_id": obtaining,
            "version_hash": snapshot["hash"],
            "status": status,
            "operation_id": operation,
            "documents": [{"id": d["id"], "hash": fingerprint(d)} for d in snapshot["documents"]],
            "checked_at": now(),
        }
        key = "agreements:journal:" + fingerprint([account, category, obtaining, snapshot["hash"]])
        if operation is None:
            value["operation_id"] = self.store.setting(key, {}).get("operation_id")
        self.store.set_setting(key, value)

    async def ensure(self, category, obtaining, snapshot):
        """Fast path completes inline via the same claimed Outbox executor.

        Waiting/unknown results remain durable; no timeout initiates a retry.
        A short background wizard-resumer later performs read-only reconciliation.
        """
        from app.database.store import Conflict
        from app.services.products import Products
        from app.workers.outbox import Executor, OutboxWorker

        account = self.authority()
        conditions = snapshot
        for _ in range(3):
            self.authority(account)
            rows = self.store.rows(
                "SELECT * FROM outbox WHERE kind='agreements' AND target=? "
                "AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') ORDER BY created_at LIMIT 1",
                (category,),
            )
            operation = rows[0] if rows else None
            if operation:
                payload = json.loads(operation["payload"])
                if not payload.get("automatic") or payload.get("account_id") != account:
                    raise ValidationError(
                        "Для категории есть другое незавершённое принятие; требуется сверка"
                    )
                if payload.get("obtaining_type_id") != obtaining:
                    return {**conditions, "automatic": True, "auto_status": "WAITING_OTHER_METHOD"}
                if operation["status"] == "UNCERTAIN":
                    if conditions["pending"]:
                        self.journal(category, obtaining, conditions, "UNCERTAIN", operation["id"])
                        return {**conditions, "automatic": True, "auto_status": "UNCERTAIN"}
                    # Server now explicitly reports no unaccepted requirements.
                    # Never reset to PENDING or resend a mutation after lost ACK.
                    from app.database.store import encode

                    self.store.execute(
                        "UPDATE outbox SET status='SUCCEEDED',result=?,error_code=NULL WHERE id=? AND status='UNCERTAIN'",
                        (encode({"server_no_pending": True, "reconciled": True}), operation["id"]),
                    )
                    operation = None
            if not conditions["pending"] and operation is None:
                self.journal(category, obtaining, conditions, "SERVER_CONFIRMED")
                return {**conditions, "automatic": True, "auto_confirmed": True}
            if operation is None:
                acceptance = {
                    "automatic": True,
                    "account_id": account,
                    "obtaining_type_id": obtaining,
                    "agreements": conditions["pending"],
                    "conditions_hash": conditions["hash"],
                }
                key = "auto-agreements:" + fingerprint([account, category, acceptance])
                try:
                    import time

                    from app.database.store import encode

                    with self.store.transaction() as db:
                        existing = db.execute(
                            "SELECT id FROM outbox WHERE operation_key=?", (key,)
                        ).fetchone()
                        budget_key = "agreements:churn:" + fingerprint([account, category, obtaining])
                        budget = self.store.setting(budget_key, {})
                        if time.time() - budget.get("since", 0) >= 600:
                            budget = {"since": time.time(), "count": 0}
                        if not existing and budget.get("count", 0) >= 6:
                            return {**conditions, "automatic": True, "auto_status": "VERSION_CHANGE_LIMIT"}
                        ident = self.store.enqueue(key, "agreements", category, acceptance, db=db)
                        if not existing:
                            budget["count"] = budget.get("count", 0) + 1
                            db.execute(
                                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (budget_key, encode(budget)),
                            )
                except Conflict:
                    return {**conditions, "automatic": True, "auto_status": "WAITING"}
                operation = self.store.rows("SELECT * FROM outbox WHERE id=?", (ident,))[0]
            ident = operation["id"]
            self.journal(category, obtaining, conditions, operation["status"], ident)
            if operation["status"] in {"PENDING", "RETRYABLE_FAILED"}:
                await OutboxWorker(
                    self.store, Executor(self.store, self.api, Products(self.store, self.api))
                ).once(
                    kinds=("agreements",),
                    operation_id=ident,
                )
            operation = self.store.rows("SELECT * FROM outbox WHERE id=?", (ident,))[0]
            fresh = await Products(self.store, self.api).schema_details(
                category, obtaining, auto_agreements=False
            )
            conditions = fresh["conditions"]
            if operation["status"] == "PERMANENT_FAILED":
                if operation["error_code"] in {"AGREEMENT_VERSION_CHANGED", "AGREEMENT_ALREADY_ACCEPTED"}:
                    continue
                return {**conditions, "automatic": True, "auto_status": operation["error_code"] or "FAILED"}
            if operation["status"] == "PROCESSING":
                return {**conditions, "automatic": True, "auto_status": "PROCESSING"}
        return {**conditions, "automatic": True, "auto_status": "VERSION_CHANGE_LIMIT"}

    def before_dispatch(self, action):
        """Thread-side final read using the SAME native SafeAccount client."""
        from app.playerok.adapter import APIError, plain, validate_page

        self.authority(action["payload"].get("account_id"))
        expected = action["payload"].get("current_agreement")
        if not expected:
            raise ValidationError("Нет актуального соглашения для отправки")
        cursor, seen, records = None, [], {}
        for _ in range(10):
            result = plain(
                self.api.account.get_game_category_agreements(
                    game_category_id=action["target"],
                    user_id=self.api.account.id,
                    count=24,
                    after_cursor=cursor,
                )
            )
            values, cursor = validate_page(result, "agreements", seen)
            for value in values:
                record = agreement_record(value)
                if record["game_category_id"] not in {None, action["target"]}:
                    raise ValidationError("Соглашение другой категории")
                if record["game_category_obtaining_type_id"] not in {
                    None,
                    action["payload"].get("obtaining_type_id"),
                }:
                    continue
                if record["id"] in records and records[record["id"]] != record:
                    raise APIError("AGREEMENT_VERSION_CHANGED")
                records[record["id"]] = record
            if not cursor:
                break
            seen.append(cursor)
        else:
            raise ValidationError("Превышен лимит страниц соглашений")
        if expected["id"] not in records:
            raise APIError("AGREEMENT_ALREADY_ACCEPTED")
        if records[expected["id"]] != expected:
            raise APIError("AGREEMENT_VERSION_CHANGED")

    async def execute(self, executor, action):
        from app.database.store import encode
        from app.playerok.adapter import APIError

        self.authority(action["payload"].get("account_id"))
        category, method = action["target"], action["payload"].get("obtaining_type_id")
        accepted, versions = [], {}
        for _ in range(20):
            try:
                details = await executor.products.schema_details(category, method, auto_agreements=False)
            except APIError as error:
                raise APIError(error.code, uncertain=bool(accepted) or error.uncertain) from None
            conditions = details["conditions"]
            if not conditions["pending"]:
                self.journal(category, method, conditions, "SERVER_CONFIRMED", action["id"])
                return {"accepted_ids": accepted, "server_no_pending": True}
            entry = conditions["pending"][0]
            seen = versions.setdefault(entry["id"], set())
            seen.add(fingerprint(entry))
            if len(seen) > 3:
                raise APIError("AGREEMENTS_VERSION_CHANGE_LIMIT")
            action["payload"]["current_agreement"] = entry
            self.store.execute(
                "UPDATE outbox SET payload=? WHERE id=? AND lease_token=? AND status='PROCESSING'",
                (encode(action["payload"]), action["id"], action["lease_token"]),
            )
            self.journal(category, method, conditions, "DISPATCH_INTENT", action["id"])
            result = await executor.mutate(
                action,
                "acceptGameCategoryAgreement",
                "accept_game_category_agreement",
                agreement_id=entry["id"],
            )
            if not isinstance(result, dict) or result.get("id") != entry["id"]:
                raise APIError("AGREEMENT_RESULT_UNKNOWN", uncertain=True)
            # A success body alone does not prove server requirements cleared.
            try:
                remaining = await Agreements(self.api).pending(category, method)
            except APIError as error:
                raise APIError(error.code, uncertain=True) from None
            if any(value["id"] == entry["id"] and value == entry for value in remaining):
                raise APIError("AGREEMENT_ACCEPTANCE_NOT_CONFIRMED", uncertain=True)
            accepted.append(entry["id"])
        raise APIError("AGREEMENTS_VERSION_CHANGE_LIMIT")
