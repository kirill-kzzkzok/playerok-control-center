import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.playerok.adapter import APIError, SafeAccount
from app.services.agreements import AutoAgreements
from app.services.products import Products, ValidationError
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard
from app.workers.agreements import AgreementResumer
from app.workers.outbox import Executor, OutboxWorker
from tests.test_agreements import AgreementAccount, AgreementAPI, agreement
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class AutoAccount(AgreementAccount):
    auto_accept_agreements = True
    connection_verified = True
    verified_account_id = "seller"

    @contextmanager
    def permit(self, operation):
        assert operation == "acceptGameCategoryAgreement"
        if not self.auto_accept_agreements or not self.connection_verified:
            raise APIError("WRITE_NOT_AUTHORIZED")
        yield


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "auto.sqlite")
    store.claim_first_owner(7)
    store.set_setting("playerok:bound_account_id", "seller")
    api = AgreementAPI()
    api.account = AutoAccount(api)
    products = Products(store, api)
    yield SimpleNamespace(store=store, api=api, products=products)
    store.close()


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_auto_accept_requires_no_pages_and_no_global_writes(rig, count):
    r = rig
    r.api.pending = [agreement(str(i)) for i in range(count)]
    r.api.category["agreements"] = deepcopy(r.api.pending)
    details = await r.products.schema_details("category", None)
    assert details["conditions"]["auto_confirmed"]
    assert r.api.account.accepted == [str(i) for i in range(count)]
    assert not r.api.account.writes_enabled
    assert all(row["status"] == "SUCCEEDED" for row in r.store.rows("SELECT status FROM outbox"))
    again = await r.products.schema_details("category", None)
    assert again["conditions"]["auto_confirmed"] and len(r.api.account.accepted) == count
    assert r.store.rows("SELECT value FROM settings WHERE key LIKE 'agreements:journal:%'")


async def test_main_dispatcher_category_choice_continues_without_terms_screen(rig):
    r = rig
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    key = StorageKey(bot_id=123, chat_id=7, user_id=7)
    state = FSMContext(SQLiteStorage(r.store), key)
    ident = r.store.draft(7, {"step": "obtaining", "category_id": "category", "fields": {}, "photos": []})
    await state.set_state("wizard")
    await state.set_data({"draft_id": ident, "revision": 0})
    try:
        await Interaction(bot, dispatcher(r.store, r.api)).click("draft:" + ident)
        value = r.store.get_draft(ident, 7)["payload"]
        assert value["step"] == "fields" and value["conditions"]["auto_confirmed"]
        assert "страниц" not in (session.calls[-1].text or "")
        assert r.api.account.accepted == ["a"]
    finally:
        await bot.session.close()


async def test_unknown_acceptance_never_replayed_and_restart_readback_resumes_wizard(rig):
    r = rig
    r.api.account.fail_id = "a"
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    ident = r.store.draft(7, {"step": "obtaining", "category_id": "category", "fields": {}, "photos": []})
    await state.set_state("wizard")
    await state.set_data({"draft_id": ident, "revision": 0})
    wizard = Wizard(r.store, r.api, r.products)
    try:
        await wizard.screen(7, state)
        assert r.store.get_draft(ident, 7)["payload"]["step"] == "agreements_wait"
        assert r.store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
        for _ in range(2):
            await r.products.schema_details("category", None)
        assert r.api.account.accepted == ["a"]
        r.api.pending = []  # independent server confirmation after a delayed acceptance
        dp = dispatcher(r.store, r.api)  # new Dispatcher and same durable SQLite FSM
        await AgreementResumer(r.store, r.api, r.products, dp, bot).once()
        assert r.store.get_draft(ident, 7)["payload"]["step"] == "fields"
        assert r.store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
        assert r.api.account.accepted == ["a"]
        assert "Условия Playerok подтверждены" in bot.session.calls[-1].text
        await AgreementResumer(r.store, r.api, r.products, dp, bot).once()
        assert len(bot.session.calls) == 1
    finally:
        await bot.session.close()


async def test_other_pending_outbox_kinds_never_claimed(rig):
    r = rig
    for kind in ("delete", "reply", "gift_send", "publish", "delivery"):
        r.store.enqueue("blocked-" + kind, kind, "other-" + kind, {})
    await r.products.schema_details("category", None)
    assert all(
        v["status"] == "PENDING" and v["attempts"] == 0
        for v in r.store.rows("SELECT * FROM outbox WHERE kind!='agreements'")
    )
    assert not await OutboxWorker(r.store, Executor(r.store, r.api, r.products)).once()


async def test_account_method_category_and_unknown_types_fail_closed(rig):
    r = rig
    r.api.account.id = "different"
    with pytest.raises(ValidationError):
        await r.products.schema_details("category", None)
    assert not r.api.account.accepted
    r.api.account.id = "seller"
    with pytest.raises(ValidationError):
        await r.products.schema_details("category", "wrong-method")
    r.api.pending[0]["game_category_id"] = "wrong-category"
    with pytest.raises(ValidationError):
        await r.products.schema_details("category", None)
    r.api.pending[0] = agreement()
    r.api.pending[0]["type"] = "PAYMENT"
    with pytest.raises(ValidationError):
        await r.products.schema_details("category", None)
    assert not r.api.account.accepted


async def test_two_workers_accept_once(rig):
    r = rig
    first, second = await asyncio.gather(
        r.products.schema_details("category", None), r.products.schema_details("category", None)
    )
    assert r.api.account.accepted == ["a"]
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    assert first["conditions"].get("auto_confirmed") or second["conditions"].get("auto_confirmed")


async def test_new_version_auto_accepted_and_journalled(rig):
    r = rig
    await r.products.schema_details("category", None)
    r.api.pending = [agreement("new-version", "Updated terms")]
    r.api.category["agreements"] = deepcopy(r.api.pending)
    await r.products.schema_details("category", None)
    assert r.api.account.accepted == ["a", "new-version"]
    journals = [
        json.loads(row["value"])
        for row in r.store.rows("SELECT value FROM settings WHERE key LIKE 'agreements:journal:%'")
    ]
    assert len({v["version_hash"] for v in journals}) == 2
    assert all("Updated terms" not in json.dumps(v) for v in journals)


async def test_narrow_safeaccount_permit_cannot_enable_any_other_mutation():
    seen = []

    def respond(request):
        data = json.loads(request.content)
        seen.append(data["operationName"])
        return httpx.Response(200, json={"data": {"acceptGameCategoryAgreement": {"id": "a"}}})

    a = SafeAccount(
        "token=fake",
        writes_enabled=False,
        auto_accept_agreements=True,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    a.id = "seller"
    a._is_initiated = True
    a.connection_verified = True
    a.verified_account_id = "seller"
    try:
        with a.permit("acceptGameCategoryAgreement"):
            assert a.accept_game_category_agreement("a") == {"id": "a"}
        for operation in (
            "createItem",
            "removeItem",
            "publishItem",
            "createChatMessage",
            "updateDeal",
            "updateItem",
        ):
            with pytest.raises(APIError), a.permit(operation):
                pass
        a.verified_account_id = "other"
        with pytest.raises(APIError), a.permit("acceptGameCategoryAgreement"):
            pass
        assert seen == ["acceptGameCategoryAgreement"] and not a.writes_enabled
    finally:
        a.close()


async def test_policy_off_retains_manual_mode_without_mutation(rig):
    r = rig
    r.api.account.auto_accept_agreements = False
    result = await r.products.schema_details("category", None)
    assert not result["conditions"].get("automatic") and not r.api.account.accepted
    assert not r.store.rows("SELECT * FROM outbox")


async def test_native_thread_fence_reloads_changed_version_before_any_post(rig):
    r = rig
    posts = []
    changed = False

    def response(request):
        nonlocal changed
        if request.method == "GET":
            assert request.url.params["operationName"] == "gameCategoryAgreements"
            if not changed:
                changed = True
                r.api.pending[0]["description"] = "Fresh authoritative version"
                r.api.category["agreements"] = deepcopy(r.api.pending)
            nodes = [
                {
                    "node": {
                        "id": v["id"],
                        "description": v["description"],
                        "gameCategoryId": "category",
                        "gameCategoryObtainingTypeId": None,
                    }
                }
                for v in r.api.pending
            ]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "gameCategoryAgreements": {
                            "edges": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "totalCount": len(nodes),
                        }
                    }
                },
            )
        data = json.loads(request.content)
        assert data["operationName"] == "acceptGameCategoryAgreement"
        assert r.api.pending[0]["description"] == "Fresh authoritative version"
        posts.append(data)
        r.api.pending = []
        return httpx.Response(200, json={"data": {"acceptGameCategoryAgreement": {"id": "a"}}})

    account = SafeAccount(
        "token=fake",
        auto_accept_agreements=True,
        client=httpx.Client(transport=httpx.MockTransport(response)),
    )
    account.id = "seller"
    account._is_initiated = True
    account.connection_verified = True
    account.verified_account_id = "seller"
    r.api.account = account
    try:
        details = await r.products.schema_details("category", None)
        assert details["conditions"]["auto_confirmed"] and len(posts) == 1
        states = r.store.rows("SELECT status,error_code FROM outbox")
        assert any(v["error_code"] == "AGREEMENT_VERSION_CHANGED" for v in states)
        assert any(v["status"] == "SUCCEEDED" for v in states)
        assert not account.writes_enabled
    finally:
        account.close()


async def test_failure_before_queue_and_expired_lease_never_sends_again(rig):
    r = rig
    original = r.api.pages

    async def offline(method, collection, **kwargs):
        if method == "get_game_category_agreements":
            raise APIError("NETWORK_ERROR")
        async for page in original(method, collection, **kwargs):
            yield page

    r.api.pages = offline
    with pytest.raises(APIError):
        await r.products.schema_details("category", None)
    assert not r.api.account.accepted and not r.store.rows("SELECT * FROM outbox")
    r.api.pages = original
    snapshot = (await r.products.schema_details("category", None, auto_agreements=False))["conditions"]
    r.store.enqueue(
        "restart",
        "agreements",
        "category",
        {
            "automatic": True,
            "account_id": "seller",
            "obtaining_type_id": None,
            "agreements": snapshot["pending"],
        },
    )
    r.store.claim(lease_seconds=-1)
    assert r.store.recover() == 1
    second = Store(r.store.path)
    try:
        details = await Products(second, r.api).schema_details("category", None)
        assert details["conditions"]["auto_status"] == "UNCERTAIN"
        assert not r.api.account.accepted
        r.api.pending = []
        assert (await Products(second, r.api).schema_details("category", None))["conditions"][
            "auto_confirmed"
        ]
        assert second.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    finally:
        second.close()


async def test_persistent_churn_limit_survives_new_service_instance(rig):
    r = rig
    for i in range(7):
        r.api.pending = [agreement(str(i), "Version " + str(i))]
        r.api.category["agreements"] = deepcopy(r.api.pending)
        value = await Products(r.store, r.api).schema_details("category", None)
    assert value["conditions"]["auto_status"] == "VERSION_CHANGE_LIMIT"
    assert r.api.account.accepted == [str(i) for i in range(6)]
    assert not (await r.products.schema_details("category", None))["conditions"].get("auto_confirmed")
    assert len(r.api.account.accepted) == 6


async def test_optional_terms_view_returns_without_reading_all_pages(rig):
    r = rig
    wizard = Wizard(r.store, r.api, r.products)
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=1, chat_id=7, user_id=7))
    details = await r.products.schema_details("category", None)
    ident = r.store.draft(
        7,
        {
            "step": "conditions",
            "manual_conditions_view": True,
            "conditions_return": "fields",
            "conditions": details["conditions"],
            "conditions_page": 0,
            "fields": {},
            "form": details["form"],
            "field_index": 0,
            "photos": [],
            "category_id": "category",
        },
    )
    await state.set_state("wizard")
    await state.set_data({"draft_id": ident, "revision": 0})
    await wizard.screen(7, state)
    nonce = (await state.get_data())["view_token"]
    await wizard.callback(7, state, "w:" + nonce + ":conditions_done")
    assert r.store.get_draft(ident, 7)["payload"]["step"] == "fields"
    assert r.api.account.accepted == ["a"]


async def test_final_fence_rejects_conflicting_duplicate_after_first_matching_id(rig):
    r = rig
    current = agreement()
    r.api.account.get_game_category_agreements = lambda **k: {
        "agreements": [current, agreement("a", "Conflicting replacement")],
        "page_info": {"has_next_page": False},
    }
    with pytest.raises(APIError, match="AGREEMENT_VERSION_CHANGED"):
        AutoAgreements(r.store, r.api).before_dispatch(
            {
                "target": "category",
                "payload": {"account_id": "seller", "current_agreement": current, "obtaining_type_id": None},
            }
        )
    assert not r.api.account.accepted


async def test_diagnostics_terms_inspector_never_accepts_new_pending_terms(rig):
    r = rig
    await r.products.schema_details("category", None)
    r.api.pending = [agreement("not-accepted-yet", "Current optional text")]
    r.api.category["agreements"] = deepcopy(r.api.pending)
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, dispatcher(r.store, r.api))
    try:
        await ui.click("diagnostics")
        assert any(
            b.callback_data == "agv:list:0"
            for row in bot.session.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        await ui.click("agv:list:0")
        choice = next(
            b.callback_data
            for row in bot.session.calls[-1].reply_markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("agv:") and ":scope:" in b.callback_data
        )
        await ui.click(choice)
        for _ in range(5):
            if "Current optional text" in bot.session.calls[-1].text:
                break
            following = next(
                b.callback_data
                for row in bot.session.calls[-1].reply_markup.inline_keyboard
                for b in row
                if b.text == "Далее"
            )
            await ui.click(following)
        assert "Current optional text" in bot.session.calls[-1].text
        assert r.api.account.accepted == ["a"] and r.api.pending[0]["id"] == "not-accepted-yet"
    finally:
        await bot.session.close()


async def test_final_pending_read_cannot_outlive_outbox_lease_and_still_post(rig):
    r = rig
    reads = 0
    posts = []

    def respond(request):
        nonlocal reads
        if request.method == "GET":
            reads += 1
            if reads == 2:
                r.store.execute(
                    "UPDATE outbox SET lease_until=0 WHERE kind='agreements' AND status='PROCESSING'"
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "gameCategoryAgreements": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "a",
                                        "description": agreement()["description"],
                                        "gameCategoryId": "category",
                                        "gameCategoryObtainingTypeId": None,
                                    }
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "totalCount": 1,
                        }
                    }
                },
            )
        posts.append(json.loads(request.content))
        r.api.pending = []
        return httpx.Response(200, json={"data": {"acceptGameCategoryAgreement": {"id": "a"}}})

    account = SafeAccount(
        "token=fake", auto_accept_agreements=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id = "seller"
    account._is_initiated = True
    account.connection_verified = True
    account.verified_account_id = "seller"
    r.api.account = account
    try:
        result = await r.products.schema_details("category", None)
        assert reads == 2
        assert not posts
        assert not result["conditions"].get("auto_confirmed")
    finally:
        account.close()
