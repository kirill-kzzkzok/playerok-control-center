"""Offline inspection of the retired technical-listing test journal.

The old create/edit/automatic-delete scenario was rejected by the marketplace and
must never be retried. This command has no credentials, network or write path.
New authorized live acceptance uses a separately reviewed real-goods scenario.
"""

import argparse
import json
from pathlib import Path

JOURNAL = Path("data/controlled_live_test.json")
MAX_JOURNAL_BYTES = 65_536
STATES = {
    "PREPARING",
    "CREATE_IN_FLIGHT",
    "CREATED",
    "TESTED",
    "FAILED",
    "UNCERTAIN",
    "CLEANUP_IN_FLIGHT",
    "CLEANED",
    "NEEDS_CLEANUP",
    "REJECTED",
}
REJECTION_REASON = "Playerok rejected technical listing as not compliant with product rules"


def inspect_journal(path=JOURNAL):
    """Return bounded historical facts only; no raw text, IDs, errors or credentials."""
    report = {
        "mode": "OFFLINE_HISTORICAL_JOURNAL",
        "legacy_write_scenario": "RETIRED",
        "live_writes_available": False,
        "retry_allowed": False,
        "external_requests": 0,
        "journal_status": "MISSING",
    }
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            report["journal_status"] = "TOO_LARGE"
            return report
        journal = json.loads(raw.decode("utf-8"))
        if not isinstance(journal, dict):
            report["journal_status"] = "INVALID"
            return report
    except FileNotFoundError:
        return report
    except (ValueError, UnicodeError, RecursionError):
        report["journal_status"] = "INVALID"
        return report
    except OSError:
        report["journal_status"] = "READ_ERROR"
        return report
    state = journal.get("state")
    report.update(
        journal_status="OK",
        state_recorded=state if isinstance(state, str) and state in STATES else "UNKNOWN",
        rejection_recorded=journal.get("reason") == REJECTION_REASON,
        remote_id_recorded=isinstance(journal.get("remote_id"), str) and bool(journal["remote_id"]),
        failure_recorded=bool(journal.get("error_code") or journal.get("cleanup_error_code")),
        cleanup_verified_recorded=journal.get("cleanup_verified") is True,
        edit_verified_recorded=journal.get("edit_verified") is True,
    )
    # Recorded fields cannot prove current remote existence/absence or authorize cleanup.
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=JOURNAL, help="Local historical journal to inspect")
    args = parser.parse_args(argv)
    report = inspect_journal(args.journal)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["journal_status"] in {"OK", "MISSING"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
