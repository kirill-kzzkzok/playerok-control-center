"""Evidence-based outcomes; HTTP codes alone never prove mutation rejection."""

from enum import StrEnum


class DispatchOutcome(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    CONFIRMED_REJECTED = "CONFIRMED_REJECTED"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    UNKNOWN = "UNKNOWN"


AUTOMATIC_DELIVERY_KINDS = frozenset({"delivery", "delivery_text", "chain_message", "fulfill"})


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
