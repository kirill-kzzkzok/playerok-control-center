"""The explicitly authorised acceptance cannot expand to another recipient/spend."""

from types import SimpleNamespace

import pytest

from app.database.store import Store
from app.gifts.mtproto import GiftFailure, PersonalGiftSender
from tools.controlled_heart_acceptance import HEART, BoundedSender


@pytest.mark.parametrize("mismatch", ["recipient", "gift", "price", "payer", "intent"])
async def test_live_wrapper_rejects_scope_before_rpc(tmp_path, monkeypatch, mismatch):
    store = Store(tmp_path / "isolated.sqlite")
    sender = BoundedSender(SimpleNamespace(), store, 99, 55)
    sender.me = SimpleNamespace(id=98 if mismatch == "payer" else 99)
    prepared = SimpleNamespace(
        gift_id=HEART + (mismatch == "gift"),
        stars=16 if mismatch == "price" else 15,
        recipient=SimpleNamespace(user_id=56 if mismatch == "recipient" else 55),
    )
    calls = []

    async def pay(*args):
        calls.append(args)

    monkeypatch.setattr(PersonalGiftSender, "pay", pay)
    try:
        with pytest.raises(GiftFailure):
            await sender.pay(prepared)
        assert calls == []
        assert not store.setting("controlled-live:attempted")
    finally:
        store.close()


async def test_live_wrapper_consumed_admission_never_calls_rpc_again(tmp_path, monkeypatch):
    store = Store(tmp_path / "isolated.sqlite")
    store.set_setting("controlled-live:attempted", True)
    sender = BoundedSender(SimpleNamespace(), store, 99, 55)
    sender.me = SimpleNamespace(id=99)
    prepared = SimpleNamespace(gift_id=HEART, stars=15, recipient=SimpleNamespace(user_id=55))
    calls = []

    async def pay(*args):
        calls.append(args)

    monkeypatch.setattr(PersonalGiftSender, "pay", pay)
    try:
        with pytest.raises(GiftFailure, match="ALREADY_ATTEMPTED"):
            await sender.pay(prepared)
        assert calls == []
    finally:
        store.close()


async def test_whole_authorised_orchestrator_stays_fake_and_single_attempt(tmp_path, monkeypatch):
    from dataclasses import replace

    from app.gifts.settings import GiftSettings
    from app.localvpn import active_proxy
    from tests.test_gifts import MTProtoTransport
    from tools import controlled_heart_acceptance as live

    monkeypatch.chdir(tmp_path)
    production = tmp_path / "owner.sqlite"
    store = Store(production)
    store.set_setting("gifts:personal_account_id", 99)
    store.close()
    session = tmp_path / "dummy.session"
    session.touch()
    settings = GiftSettings(
        1,
        "fake-hash",
        str(session),
        "stars",
        "gift",
        "username-field",
        False,
        15,
        15,
        telegram_transport="localvpn",
    )
    config = SimpleNamespace(
        telegram_token="synthetic", playerok_header="synthetic", database_path=production
    )
    remote = MTProtoTransport()
    remote.recipient.username = "kzzkzok"

    class Proxy:
        ready = True

        def __init__(self, settings):
            self.state = {"generation": 1}

        async def confirm(self):
            return 1

        async def __aenter__(self):
            self.token = active_proxy.set(self)
            return self

        async def __aexit__(self, *args):
            active_proxy.reset(self.token)

    initial = BoundedSender.__init__

    def fake_init(self, *args):
        initial(self, *args)
        self.client = remote

    monkeypatch.setattr(live.Config, "load", lambda: config)
    monkeypatch.setattr(live.GiftSettings, "load", lambda: settings)
    monkeypatch.setattr(live.NetworkSettings, "load", lambda: None)
    monkeypatch.setattr(live, "PlayerokProxy", Proxy)
    monkeypatch.setattr(live, "PersonalGiftSender", lambda s: PersonalGiftSender(replace(s), client=remote))
    monkeypatch.setattr(BoundedSender, "__init__", fake_init)
    monkeypatch.setattr(live, "configure_logging", lambda *args: None)
    monkeypatch.setattr(live, "JOURNAL", tmp_path / "acceptance.sqlite")
    monkeypatch.setattr(live, "REPORT", tmp_path / "acceptance.json")
    result = await live.run(send=True)
    assert result["status"] == "LIVE_SUCCESS", result  # Fake receipt, explicitly no network.
    assert result["payment_attempts"] == len(remote.payments) == 1
    assert result["mock_buyer_notices"] == 1
    assert result["mock_notice_text"] == "Все отправлено, спасибо за покупку!"
    assert result["repeat_worker_executed"] is False
    second = await live.run(send=True)
    assert second["code"] == "CONTROLLED_LIVE_JOURNAL_ALREADY_EXISTS"
    assert len(remote.payments) == 1
    assert '"status": "LIVE_SUCCESS"' in live.REPORT.read_text(encoding="utf-8")
