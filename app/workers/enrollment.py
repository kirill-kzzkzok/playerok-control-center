import asyncio
import json
import time
import uuid
from datetime import datetime

from app.database.store import Conflict, now, server_time
from app.playerok.adapter import APIError
from app.services.automation import future_order
from app.services.delivery_chains import DeliveryChains
from app.services.products import ValidationError


class EnrollmentWorker:
    """Enroll opted-in future sales only. Never infer a server creation date."""

    def __init__(self, store, api):
        self.store, self.api, self.chains = store, api, DeliveryChains(store, api)

    def discover(self):
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT o.id,o.payload,p.updated_at,p.revision FROM orders o JOIN delivery_policies p ON p.item_id=o.item_id "
                "WHERE (o.status='PAID' OR (json_extract(p.payload,'$.mode')='playerok' AND o.status IN ('SENT','CONFIRMED','CONFIRMED_AUTOMATICALLY'))) AND json_extract(p.payload,'$.enabled')=1 AND json_extract(p.payload,'$.auto_enroll')=1 "
                "AND NOT EXISTS(SELECT 1 FROM auto_enrollment e WHERE e.order_id=o.id AND e.policy_revision=p.revision) "
                "AND NOT EXISTS(SELECT 1 FROM delivery_chains c WHERE c.order_id=o.id) ORDER BY o.created_at,o.id LIMIT 20"
            ).fetchall()
            for row in rows:
                eligible = future_order(json.loads(row["payload"]).get("created_at"), row["updated_at"])
                db.execute(
                    "INSERT INTO auto_enrollment(order_id,policy_revision,status,error_code,updated_at) VALUES(?,?,?,?,?)",
                    (
                        row["id"],
                        row["revision"],
                        "PENDING" if eligible else "SKIPPED",
                        None if eligible else "NOT_A_VERIFIED_FUTURE_ORDER",
                        now(),
                    ),
                )

    def claim(self):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT e.* FROM auto_enrollment e WHERE status='PENDING' AND due_at<=? AND (lease_until IS NULL OR lease_until<?) ORDER BY due_at,updated_at,order_id LIMIT 1",
                (time.time(), time.time()),
            ).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE auto_enrollment SET lease_token=?,lease_until=?,attempts=attempts+1 WHERE order_id=? AND policy_revision=?",
                (token, time.time() + 180, row["order_id"], row["policy_revision"]),
            )
            return {**dict(row), "lease_token": token}

    async def once(self):
        if not self.api.account.writes_enabled or not getattr(self.api.account, "automation_enabled", False):
            self.store.set_setting("enrollment:health", {"status": "DISABLED", "at": now()})
            return 0
        if not self.store.setting("orders:last_complete_sync"):
            self.store.set_setting("enrollment:health", {"status": "WAITING_ORDER_BASELINE", "at": now()})
            return 0
        self.discover()
        processed = 0
        for _ in range(4):
            row = self.claim()
            if not row:
                break
            order_id, revision = row["order_id"], row["policy_revision"]
            status, code, delay = "CREATED", None, 0
            try:
                observed = self.store.rows(
                    "SELECT json_extract(payload,'$.created_at') AS created FROM orders WHERE id=?",
                    (order_id,),
                )
                created = server_time(observed[0]["created"]) if observed else None
                if not created or time.time() - datetime.fromisoformat(created).timestamp() > 900:
                    raise Conflict("OWNER_REVIEW_REQUIRED")
                policies = self.store.rows(
                    "SELECT p.actor,p.payload,p.revision FROM orders o JOIN delivery_policies p ON p.item_id=o.item_id WHERE o.id=?",
                    (order_id,),
                )
                if (
                    not policies
                    or policies[0]["revision"] != revision
                    or not json.loads(policies[0]["payload"]).get("auto_enroll")
                ):
                    raise Conflict("POLICY_CHANGED")
                existing = self.store.rows("SELECT 1 FROM delivery_chains WHERE order_id=?", (order_id,))
                result = (
                    order_id
                    if existing
                    else await self.chains.create(policies[0]["actor"], order_id, revision, automatic=True)
                )
                if result is None:
                    status, code = "BLOCKED", "STOCK_EMPTY"
            except APIError as error:
                status, code = "PENDING", error.code
                delay = min(900, 30 * 2 ** min(row["attempts"], 5))
                if str(error.retry_after or "").isdigit():
                    delay = max(delay, int(error.retry_after))
            except (Conflict, ValidationError):
                status, code = "BLOCKED", "OWNER_REVIEW_REQUIRED"
            with self.store.transaction() as db:
                changed = db.execute(
                    "UPDATE auto_enrollment SET status=?,error_code=?,due_at=?,lease_until=NULL,lease_token=NULL,updated_at=? WHERE order_id=? AND policy_revision=? AND lease_token=?",
                    (status, code, time.time() + delay, now(), order_id, revision, row["lease_token"]),
                ).rowcount
                if changed and status == "BLOCKED":
                    from app.database.store import encode

                    db.execute(
                        "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'enrollment_blocked',?,?,?)",
                        (
                            f"enrollment_blocked:{order_id}:{revision}",
                            encode({"order_id": order_id, "code": code}),
                            now(),
                            now(),
                        ),
                    )
            processed += 1
        self.store.set_setting("enrollment:health", {"status": "ACTIVE", "processed": processed, "at": now()})
        return processed

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.once()
            except Exception as error:  # noqa: BLE001 -- retain durable jobs/leases; never log raw API text
                self.store.set_setting(
                    "enrollment:health", {"status": "ERROR", "code": type(error).__name__, "at": now()}
                )
            try:
                await asyncio.wait_for(stop.wait(), 10)
            except TimeoutError:
                pass
