"""One authorised acceptance, not a shop command. Default is read-only preflight.

Playerok and Telegram Bot API are ALWAYS test transports. Only PersonalGiftSender
may use the real MTProto session, locked against the shop. One fixed local journal
and a durable no-retry marker protect this particular acceptance from reruns.
"""

import argparse
import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from app.config import Config
from app.gifts.core import SEED_GIFTS
from app.gifts.mtproto import GiftFailure, PersonalGiftSender
from app.gifts.settings import GiftSettings
from app.localvpn import NetworkSettings, PlayerokProxy
from app.security.instance import InstanceLock
from app.security.logging import configure_logging
from tests.test_final_acceptance_e2e import ORDER, AcceptanceHarness

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "data/final-heart-acceptance-20261101.sqlite3"
REPORT = ROOT / "data/final-heart-acceptance-20261101.json"
HEART = SEED_GIFTS["heart"][1]


class BoundedSender(PersonalGiftSender):
    def __init__(self, settings, store, payer, recipient):
        super().__init__(settings)
        self.store, self.expected_payer, self.expected_recipient = store, payer, recipient

    async def pay(self, prepared):
        if (prepared.gift_id, prepared.stars, prepared.recipient.user_id, self.me.id) != (
            HEART,
            15,
            self.expected_recipient,
            self.expected_payer,
        ):
            raise GiftFailure("CONTROLLED_LIVE_SCOPE_MISMATCH")
        with self.store.transaction():
            if self.store.setting("controlled-live:attempted"):
                raise GiftFailure("CONTROLLED_LIVE_ALREADY_ATTEMPTED")
            steps = self.store.rows("SELECT state,spent_at FROM gift_steps")
            if len(steps) != 1 or steps[0]["state"] != "SPENDING" or not steps[0]["spent_at"]:
                raise GiftFailure("CONTROLLED_LIVE_INTENT_REQUIRED")
            # Before even the proxy confirmation, conservatively consume the only
            # admission. A crash between this record and RPC never grants a retry.
            self.store.set_setting("controlled-live:attempted", True)
        return await super().pay(prepared)


async def run(send=False):
    config, settings = Config.load(), GiftSettings.load()
    configure_logging([config.telegram_token, config.playerok_header, settings.api_hash])
    result = {
        "mode": "ISOLATED_MOCK_PLAYEROK_REAL_MTPROTO",
        "playerok_real_mutations": 0,
        "buyer_real_messages": 0,
        "payment_attempts": 0,
        "status": "BLOCKED",
    }
    harness = sender = None
    try:
        if settings.telegram_transport != "localvpn":
            raise GiftFailure("CONTROLLED_LIVE_REQUIRES_LOCALVPN")
        if not Path(settings.session_path).is_file():
            raise GiftFailure("SESSION_NOT_CONFIGURED")
        if not config.database_path.is_file():
            raise GiftFailure("EXPECTED_PERSONAL_ACCOUNT_NOT_PINNED")
        # Read the already pinned identity only; never open production with Store,
        # migrate it, copy orders, recover it, or execute its outbox.
        with sqlite3.connect(config.database_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM settings WHERE key='gifts:personal_account_id'").fetchone()
        payer = json.loads(row[0]) if row else None
        if type(payer) is not int or payer <= 0:
            raise GiftFailure("EXPECTED_PERSONAL_ACCOUNT_NOT_PINNED")
        if JOURNAL.exists():
            raise GiftFailure("CONTROLLED_LIVE_JOURNAL_ALREADY_EXISTS")
        with InstanceLock(str(config.database_path) + ".lock"):
            async with PlayerokProxy(NetworkSettings.load()):
                # Authorised session; this client cannot pay during preflight.
                sender = PersonalGiftSender(replace(settings, payment_enabled=False))
                await sender.connect()
                if sender.me.id != payer:
                    raise GiftFailure("PERSONAL_ACCOUNT_CHANGED")
                recipient = await sender.resolve("@kzzkzok")
                catalog = await sender.catalog()
                gift = next((g for g in catalog if g.id == HEART), None)
                if gift is None or gift.stars != 15 or gift.sold_out or gift.auction:
                    raise GiftFailure("HEART_NOT_AVAILABLE_AT_EXACTLY_FIFTEEN")
                balance = await sender.balance()
                if balance < 15:
                    raise GiftFailure("BALANCE_TOO_LOW")
                result.update(
                    status="READ_ONLY_PREFLIGHT_OK",
                    recipient_id=recipient.user_id,
                    payer_confirmed=True,
                    heart_stars=15,
                    balance_before=balance,
                )
                await sender.close()
                sender = None
                if not send:
                    return result
                # The persistent WAL belongs exclusively to this acceptance. Its
                # fake Playerok has no HTTP client or forwarding fallback.
                harness = AcceptanceHarness(JOURNAL)
                harness.settings = replace(
                    settings,
                    category_id="stars",
                    obtaining_type_id="gift",
                    buyer_username_field_id="username-field",
                    payment_enabled=True,
                    max_order_stars=15,
                    max_daily_stars=15,
                    refund_enabled=False,
                )
                harness.store.set_setting("gifts:personal_account_id", payer)
                harness.api.gift_settings = harness.settings
                harness.api.gift_sender = BoundedSender(
                    harness.settings, harness.store, payer, recipient.user_id
                )
                harness.attach()
                await harness.create_and_prepare()
                await harness.launch_action("commit")
                await harness.worker.once()  # Fake publication only.
                await harness.sync.once()  # Synthetic PAID only, isolated database.
                await harness.gifts.once()
                await harness.worker.once()  # Production intent -> real single RPC -> receipt.
                job = harness.service.job(ORDER)
                result["payment_attempts"] = int(bool(harness.store.setting("controlled-live:attempted")))
                result["gift_job"] = job["state"]
                result["gift_steps"] = [g["state"] for g in harness.service.steps(ORDER)]
                if job["state"] != "SUCCEEDED":
                    result.update(status="DELIVERY_NOT_CONFIRMED", code=job["error_code"])
                    return result
                result.update(
                    status="LIVE_SUCCESS",
                    stars_confirmed=15,
                    receipt=json.loads(harness.service.steps(ORDER)[0]["receipt"]),
                )
                await harness.gifts.once()
                while await harness.worker.once():
                    pass
                result["mock_buyer_notices"] = len(harness.api.sent)
                result["mock_notice_text"] = harness.api.sent[0][1] if harness.api.sent else None
                result["outbox"] = harness.store.rows("SELECT kind,status FROM outbox ORDER BY created_at")
                result["balance_after"] = await harness.api.gift_sender.balance()
                harness.restart()
                harness.api.gift_sender.store = harness.store
                await harness.gifts.once()
                result["repeat_worker_executed"] = await harness.worker.once()
                result["payment_attempts"] = int(bool(harness.store.setting("controlled-live:attempted")))
                return result
    except Exception as error:  # noqa: BLE001 -- no raw Telethon/HTTP/session exceptions
        result.update(error_type=type(error).__name__, code=getattr(error, "code", "PREFLIGHT_NOT_CONFIRMED"))
    finally:
        if harness:
            if harness.store.setting("controlled-live:attempted"):
                result["payment_attempts"] = 1
            await harness.close()
        if sender:
            await sender.close()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        report_path = REPORT
        if result.get("code") == "CONTROLLED_LIVE_JOURNAL_ALREADY_EXISTS":
            report_path = REPORT.with_suffix(".refused.json")
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-authorized-heart", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.send_authorized_heart)), ensure_ascii=False))
