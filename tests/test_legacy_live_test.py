import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from app.live_test import MAX_JOURNAL_BYTES, REJECTION_REASON, inspect_journal, main
from app.playerok.adapter import SafeAccount
from tests.transport_helpers import mutate


def test_missing_history_cannot_rearm_write_test_or_create_directory(tmp_path):
    path = tmp_path / "missing" / "journal.json"
    report = inspect_journal(path)
    assert report["journal_status"] == "MISSING"
    assert report["live_writes_available"] is report["retry_allowed"] is False
    assert report["external_requests"] == 0
    assert not path.parent.exists()


@pytest.mark.parametrize("state", ["CLEANED", "FAILED", "UNCERTAIN", "CREATE_IN_FLIGHT", "NEEDS_CLEANUP"])
def test_every_old_state_stays_readonly_and_preserves_journal(tmp_path, state):
    path = tmp_path / "journal.json"
    path.write_text(
        json.dumps(
            {
                "state": state,
                "remote_id": "old-remote-id",
                "reason": REJECTION_REASON,
                "cleanup_verified": True,
                "error_code": "raw-private-error",
                "token": "secret",
                "header": "private-cookie",
                "name": "private-name",
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    report = inspect_journal(path)
    assert path.read_bytes() == before
    assert report["state_recorded"] == state
    assert report["rejection_recorded"]
    assert report["remote_id_recorded"]
    assert report["failure_recorded"]
    assert report["cleanup_verified_recorded"]
    assert report["retry_allowed"] is False
    text = json.dumps(report)
    for value in ["old-remote-id", "raw-private-error", "private-cookie", "private-name", "secret"]:
        assert value not in text


@pytest.mark.parametrize(
    "raw,status",
    [
        (b"not json", "INVALID"),
        (b"[]", "INVALID"),
        (b"null", "INVALID"),
        (b"\xff", "INVALID"),
        (b"[" * 2000, "INVALID"),
        (b"x" * (MAX_JOURNAL_BYTES + 1), "TOO_LARGE"),
    ],
)
def test_bad_history_is_bounded_and_never_resets_it(tmp_path, raw, status):
    path = tmp_path / "journal.json"
    path.write_bytes(raw)
    report = inspect_journal(path)
    assert report["journal_status"] == status
    assert report["retry_allowed"] is False
    assert path.read_bytes() == raw


def test_unknown_and_wrong_typed_fields_cannot_be_reflected_or_grant_permission(tmp_path, capsys):
    path = tmp_path / "journal.json"
    path.write_text(
        json.dumps(
            {
                "state": ["secret"],
                "remote_id": {},
                "cleanup_verified": "true",
                "edit_verified": 1,
                "live_writes_available": True,
                "retry_allowed": True,
            }
        ),
        encoding="utf-8",
    )
    assert main(["--journal", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state_recorded"] == "UNKNOWN"
    assert report["remote_id_recorded"] is False
    assert report["cleanup_verified_recorded"] is report["edit_verified_recorded"] is False
    assert report["live_writes_available"] is report["retry_allowed"] is False


def test_read_error_does_not_show_path_or_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "open", Mock(side_effect=PermissionError("secret-file-path")))
    report = inspect_journal(tmp_path / "journal.json")
    assert report["journal_status"] == "READ_ERROR"
    assert "secret-file-path" not in json.dumps(report)


def test_cli_without_credentials_never_constructs_account_or_loads_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "app.config.Config.load", Mock(side_effect=AssertionError("must not read credentials"))
    )
    monkeypatch.setattr(
        "app.playerok.adapter.SafeAccount", Mock(side_effect=AssertionError("must not create client"))
    )
    assert main(["--journal", str(tmp_path / "absent.json")]) == 0
    assert json.loads(capsys.readouterr().out)["legacy_write_scenario"] == "RETIRED"


def test_real_cli_reports_missing_or_invalid_history_without_loading_env(tmp_path):
    path = tmp_path / "bad.json"
    for present in [False, True]:
        if present:
            path.write_text("secret invalid contents", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "app.live_test", "--journal", str(path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        assert result.returncode == (2 if present else 0)
        assert result.stderr == ""
        report = json.loads(result.stdout)
        assert report["external_requests"] == 0
        assert report["journal_status"] == ("INVALID" if present else "MISSING")
        assert "secret" not in result.stdout


@pytest.mark.parametrize("route", ["default", "mounted"])
async def test_contract_test_mutation_helper_refuses_real_transport(route):
    client = (
        httpx.Client()
        if route == "default"
        else httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("must not dispatch")),
            mounts={"https://playerok.com": httpx.HTTPTransport()},
        )
    )
    account = SafeAccount("token=test-only", writes_enabled=True, client=client)
    try:
        with pytest.raises(TypeError, match="TEST_HELPER_REQUIRES_MOCK_TRANSPORT"):
            await mutate(account, "removeItem", "remove_item", id="must-not-send")
    finally:
        account.close()
