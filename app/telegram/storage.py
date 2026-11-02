import json

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from app.database.store import encode


class SQLiteStorage(BaseStorage):
    def __init__(self, store):
        self.store = store

    def key(self, key: StorageKey):
        return encode(
            [key.bot_id, key.chat_id, key.user_id, key.thread_id, key.business_connection_id, key.destiny]
        )

    async def set_state(self, key, state: StateType = None):
        state = state.state if isinstance(state, State) else state
        self.store.execute(
            "INSERT INTO fsm(key,state) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state",
            (self.key(key), state),
        )

    async def replace(self, key, state, data):
        state = state.state if isinstance(state, State) else state
        self.store.execute(
            "INSERT INTO fsm(key,state,data) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state,data=excluded.data",
            (self.key(key), state, encode(data)),
        )

    async def get_state(self, key):
        rows = self.store.rows("SELECT state FROM fsm WHERE key=?", (self.key(key),))
        return rows[0]["state"] if rows else None

    async def set_data(self, key, data):
        self.store.execute(
            "INSERT INTO fsm(key,data) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (self.key(key), encode(data)),
        )

    async def get_data(self, key):
        rows = self.store.rows("SELECT data FROM fsm WHERE key=?", (self.key(key),))
        return json.loads(rows[0]["data"]) if rows else {}

    async def update_data(self, key, data):
        with self.store.transaction() as db:
            row = db.execute("SELECT data FROM fsm WHERE key=?", (self.key(key),)).fetchone()
            value = json.loads(row[0]) if row else {}
            value.update(data)
            db.execute(
                "INSERT INTO fsm(key,data) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (self.key(key), encode(value)),
            )
            return value.copy()

    async def close(self):
        pass  # Store is shared; lifecycle belongs to application.


async def replace_context(context, state, data):
    """Atomic entry for durable SQLite FSM; ordinary aiogram storage remains usable."""
    replace = getattr(context.storage, "replace", None)
    if replace is not None:
        await replace(context.key, state, data)
    else:
        await context.set_data(data)
        await context.set_state(state)
