"""Evidence-based outcomes; HTTP codes alone never prove mutation rejection."""

from enum import StrEnum


class DispatchOutcome(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    CONFIRMED_REJECTED = "CONFIRMED_REJECTED"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    UNKNOWN = "UNKNOWN"


def dispatch_outcome(started_at, status):
    if status == "SUCCEEDED":
        return DispatchOutcome.CONFIRMED_SUCCESS
    if started_at is None:
        return DispatchOutcome.NOT_DISPATCHED
    # No current adapter error has a documented no-effect receipt after POST.
    # CONFIRMED_REJECTED is reserved for such positive evidence, not inferred.
    return DispatchOutcome.UNKNOWN
