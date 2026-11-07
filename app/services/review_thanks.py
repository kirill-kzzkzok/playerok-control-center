from app.database.store import Conflict, encode, now, server_time

THANKS = (
    "💌 спасибо за твой отзыв! ♡\n\n"
    "очень приятно, что ты выбрал именно мой магазин. такие мелочи делают мой день чуточку теплее. 🤎\n\n"
    "🌙 буду рад видеть тебя снова!"
)


def verified_review(order, account):
    review = order.get("review")
    if not isinstance(review, dict) or not account:
        return None
    creator, recipient = review.get("creator"), review.get("user")
    if not isinstance(creator, dict) or not isinstance(recipient, dict):
        return None
    customer = (order.get("user") or {}).get("id")
    if (
        order.get("direction") != "OUT"
        or ((order.get("item") or {}).get("user") or {}).get("id") != account
        or not customer
        or customer == account
        or creator.get("id") != customer
        or recipient.get("id") != account
        or review.get("status") != "APPROVED"
        or type(review.get("rating")) is not int
        or not 1 <= review["rating"] <= 5
        or not isinstance(review.get("id"), str)
        or not review["id"]
    ):
        return None
    return review


def ingest_review(store, db, order):
    account = store.setting("playerok:account_id")
    review = verified_review(order, account)
    if not review:
        return
    observed = server_time(review.get("created_at"))
    cutoff = store.setting("reviews:thanks_since")
    if not observed or not cutoff or not cutoff <= observed <= now():
        return
    order_id = order["id"]
    key = f"review:{account}:{order_id}:{review['id']}"
    if db.execute(
        "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'review',?,?,?)",
        (
            key,
            encode(
                {
                    "id": order_id,
                    "created_at": observed,
                    "review": {"rating": review["rating"], "text": str(review.get("text") or "")[:700]},
                }
            ),
            now(),
            now(),
        ),
    ).rowcount:
        from app.security.logging import event
        from app.services.shop_events import buyer, short_title

        event(
            "⭐ ОТЗЫВ",
            f"{buyer(order)} · {short_title(order['item'].get('name'))} · {review['rating']}/5",
            order=order_id,
        )
    chat = (order.get("chat") or {}).get("id")
    actor = db.execute("SELECT user_id FROM administrators ORDER BY user_id LIMIT 1").fetchone()
    saved = db.execute(
        "SELECT json_extract(payload,'$.review.rating') FROM inbox WHERE key=?", (key,)
    ).fetchone()
    if (
        saved
        and saved[0] >= 4
        and review["rating"] >= 4
        and chat
        and actor
        and order["status"] in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
    ):
        store.enqueue(
            f"review-thanks:{account}:{order_id}",
            "reply",
            chat,
            {
                "text": THANKS,
                "review_thanks": {
                    "order": order_id,
                    "review": review["id"],
                    "account": account,
                    "buyer": order["user"]["id"],
                    "item": order["item"]["id"],
                    "actor": actor[0],
                },
            },
            db,
        )


def guard(store, api, action):
    proof = action["payload"]["review_thanks"]
    if (
        not getattr(api.account, "automation_enabled", False)
        or api.account.id != proof["account"]
        or store.setting("playerok:account_id") != proof["account"]
        or action["payload"].get("text") != THANKS
        or not store.rows("SELECT 1 FROM administrators WHERE user_id=?", (proof.get("actor"),))
    ):
        raise Conflict("Право отправки благодарности не подтверждено")
    return proof


async def verify(store, api, action):
    proof = guard(store, api, action)
    order = await api.call("get_deal", deal_id=proof["order"])
    review = verified_review(order, proof["account"])
    if (
        order.get("id") != proof["order"]
        or order.get("status") not in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
        or not review
        or review["id"] != proof["review"]
        or review["rating"] < 4
        or order["user"]["id"] != proof["buyer"]
        or order["item"]["id"] != proof["item"]
        or (order.get("chat") or {}).get("id") != action["target"]
        or order.get("has_problem") is not False
    ):
        raise Conflict("Отзыв или чат сделки не подтверждён")
    chat = await api.call("get_chat", chat_id=action["target"])
    if chat.get("id") != action["target"] or chat.get("is_texting_allowed") is not True:
        raise Conflict("Чат покупателя недоступен")
