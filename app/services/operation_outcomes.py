"""Evidence-based outcomes; HTTP codes alone never prove mutation rejection."""

import json
from enum import StrEnum


class DispatchOutcome(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    CONFIRMED_REJECTED = "CONFIRMED_REJECTED"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    UNKNOWN = "UNKNOWN"


AUTOMATIC_DELIVERY_KINDS = frozenset({"delivery", "delivery_text", "chain_message", "fulfill"})


def historical_edit_authority(db, action):
    """Do not resurrect an edit superseded by a later owner decision."""
    row = db.execute("SELECT rowid,* FROM outbox WHERE id=?", (action["id"],)).fetchone()
    if not db.execute(
        "SELECT 1 FROM operation_receipts r JOIN administrators a ON a.user_id=r.owner_id WHERE r.operation_id=?",
        (action["id"],),
    ).fetchone():
        return False
    if (
        not row
        or db.execute(
            "SELECT 1 FROM operation_resolutions WHERE operation_id=? UNION ALL "
            "SELECT 1 FROM outbox_late_results WHERE operation_id=? UNION ALL "
            "SELECT 1 FROM settings WHERE key='promotions:intent:'||? UNION ALL "
            "SELECT 1 FROM outbox WHERE target=? AND rowid>? UNION ALL "
            "SELECT 1 FROM audit_log WHERE action='STOP_ALL_DELIVERY_POLICIES' AND created_at>=?",
            (row["id"], row["id"], row["id"], row["target"], row["rowid"], row["created_at"]),
        ).fetchone()
    ):
        return False
    payload = json.loads(row["payload"])
    if payload.get("code_migration"):
        return False  # Its failed migration journal requires separate evidence.
    binding = payload.get("logical_binding")
    if binding:
        product = db.execute(
            "SELECT p.* FROM logical_products p JOIN logical_listings l ON l.product_id=p.id "
            "WHERE p.id=? AND l.item_id=? AND l.operation_id=? AND p.state='PENDING' "
            "AND p.revision=? AND p.enabled=0 AND p.updated_at<=?",
            (binding["product_id"], row["target"], row["id"], binding["revision"], row["created_at"]),
        ).fetchone()
        if not product:
            return False
        settings = payload.get("platform_settings")
        if settings:
            from app.services.platform_orders import stop_token

            return settings["control_revision"] == product["control_revision"] and settings[
                "stop_token"
            ] == stop_token(db)
        # Old binding payloads have no control-revision snapshot. Only an
        # untouched initial product provides positive authority in that case.
        return product["control_revision"] == 0
    changed = set(payload) & {"name", "description"}
    return (
        bool(changed)
        and set(payload)
        <= {"name", "description", "expected_name", "expected_description", "marketing_description"}
        and all(isinstance(payload.get("expected_" + field), str) for field in changed)
    )


def recover_historical_edits(db):
    """One upgrade retry, with positive no-dispatch evidence; no paid kinds."""
    candidates = db.execute(
        "SELECT o.* FROM outbox o WHERE o.kind='edit' AND o.status='PERMANENT_FAILED' "
        "AND o.error_code IN ('HTTP_500','HTTP_502','HTTP_503','HTTP_504','HTTP_429','NETWORK_ERROR','CONNECTION_NOT_VERIFIED') "
        "AND EXISTS(SELECT 1 FROM audit_log a WHERE a.action='DISPATCH_OUTCOME' AND a.target=o.id AND a.outcome='NOT_DISPATCHED') "
        "AND NOT EXISTS(SELECT 1 FROM audit_log a WHERE a.target=o.id AND "
        "(a.action='RECOVER_UNSTARTED_EDIT' OR (a.action='DISPATCH_OUTCOME' AND a.outcome<>'NOT_DISPATCHED')))"
    ).fetchall()
    recovered = 0
    for row in candidates:
        if not unstarted_listing(db, row) or not historical_edit_authority(db, row):
            continue
        db.execute(
            "UPDATE outbox SET status='RETRYABLE_FAILED',error_code='PREFLIGHT_INTERRUPTED',"
            "retry_at=0,lease_token=NULL,lease_until=NULL WHERE id=?",
            (row["id"],),
        )
        db.execute(
            "INSERT INTO audit_log(actor,action,target,outcome,created_at) "
            "VALUES(NULL,'RECOVER_UNSTARTED_EDIT',?,'NOT_DISPATCHED',strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (row["id"],),
        )
        recovered += 1
    return recovered


def unstarted_listing(db, action):
    """Read-preflight proof, including the independent paid-intent journal."""
    return bool(
        db.execute(
            "SELECT 1 FROM outbox o WHERE o.id=? AND o.kind IN ('edit','edit_fields','discount','premium_publish','premium_bump') "
            "AND o.started_at IS NULL AND (o.result IS NULL OR o.result='null') "
            "AND NOT EXISTS(SELECT 1 FROM outbox_late_results l WHERE l.operation_id=o.id) "
            "AND NOT EXISTS(SELECT 1 FROM settings WHERE key='promotions:intent:'||o.id) "
            "AND (o.kind NOT IN ('premium_publish','premium_bump') OR EXISTS("
            "SELECT 1 FROM promotion_expenses e WHERE e.operation_key=o.operation_key AND e.item_id=o.target "
            "AND e.status='RESERVED' AND e.amount=json_extract(o.payload,'$.amount')))",
            (action["id"],),
        ).fetchone()
    )


def unstarted_delivery(db, action):
    """Positive journal proof for free preflight, never a post-send retry."""
    return bool(
        db.execute(
            "SELECT 1 FROM outbox o WHERE o.id=? AND o.kind IN ('delivery','delivery_text','chain_message','fulfill') "
            "AND o.started_at IS NULL AND (o.result IS NULL OR o.result='null') "
            "AND NOT EXISTS(SELECT 1 FROM outbox_late_results l WHERE l.operation_id=o.id)",
            (action["id"],),
        ).fetchone()
    )


def dispatch_outcome(started_at, status, *, late_result=False):
    if status == "SUCCEEDED":
        return DispatchOutcome.CONFIRMED_SUCCESS
    if late_result:
        return DispatchOutcome.UNKNOWN
    if started_at is None:
        return DispatchOutcome.NOT_DISPATCHED
    # No current adapter error has a documented no-effect receipt after POST.
    # CONFIRMED_REJECTED is reserved for such positive evidence, not inferred.
    return DispatchOutcome.UNKNOWN
