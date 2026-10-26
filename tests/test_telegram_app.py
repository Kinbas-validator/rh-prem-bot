import asyncio
from dataclasses import replace

import pytest

from rh_prem_bot.app import App, demo_snapshot
from rh_prem_bot.config import Settings
from rh_prem_bot.lighter import Feed, now_ms
from rh_prem_bot.models import Market
from rh_prem_bot.telegram import TelegramError


class FakeTelegram:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def send(self, chat, text, reply_markup=None):
        self.calls.append(
            ("sendMessage", {"chat_id": chat, "text": text, "reply_markup": reply_markup})
        )
        if self.error:
            raise self.error
        return {"message_id": 9}

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return True

    async def edit(self, chat, message_id, text, markup):
        return await self.call(
            "editMessageText", chat_id=chat, message_id=message_id, text=text, reply_markup=markup
        )


def update(text="/start", chat=123, user=123):
    return {"message": {"chat": {"id": chat}, "from": {"id": user}, "text": text}}


def callback(data, chat=123, user=123):
    return {
        "callback_query": {
            "id": "cb1",
            "data": data,
            "from": {"id": user},
            "message": {"message_id": 99, "chat": {"id": chat}},
        }
    }


@pytest.fixture
def app(store):
    cfg = Settings(chat_ids=(123, -100), admin_ids=(123,))
    feed = Feed(None, cfg, store, [Market(42, "OPENAI", "perp"), Market(15, "NVDA", "perp")])
    return App(cfg, store, feed, FakeTelegram())


async def test_start_second_screenshot_and_menu(app):
    await app.handle_update(update())
    method, payload = app.telegram.calls[-1]
    assert method == "sendMessage" and "Премиум за <b>5м</b>" in payload["text"]
    assert len(payload["reply_markup"]["inline_keyboard"][0]) == 5
    await app.handle_update(callback("menu"))
    assert app.telegram.calls[-1][0] == "editMessageText"
    await app.handle_update(callback("rank:60"))
    assert "60м" in app.telegram.calls[-1][1]["text"]


async def test_unauthorized_chat_cannot_get_information_or_change_settings(app):
    await app.handle_update(update("/set volume 1", chat=999))
    assert not app.telegram.calls and app.cfg.min_premium_usd == 10000
    await app.handle_update(callback("toggle:5:42", chat=999))
    assert not app.store.get("muted:999")
    assert app.telegram.calls[-1][0] == "answerCallbackQuery"


async def test_group_nonadmin_cannot_change_or_mute(app):
    await app.handle_update(update("/pause", chat=-100, user=777))
    assert not app.store.get("paused", False)
    await app.handle_update(callback("toggle:5:42", chat=-100, user=777))
    assert app.store.get("muted:-100", []) == []


async def test_thresholds_and_pause_persist(app):
    await app.handle_update(update("/set volume 12345"))
    assert app.cfg.min_premium_usd == 12345
    await app.handle_update(update("/pause"))
    assert app.store.get("paused")
    new_cfg = Settings()
    App(new_cfg, app.store, app.feed, app.telegram)
    assert new_cfg.min_premium_usd == 12345
    await app.handle_update(update("/resume"))
    assert not app.store.get("paused")


async def test_reset_restores_startup_env_thresholds(app):
    before = app.cfg.min_premium_usd
    await app.handle_update(update("/set volume 12345"))
    await app.handle_update(update("/reset"))
    assert app.cfg.min_premium_usd == before
    assert app.store.get("thresholds") == {}


async def test_pause_suspends_already_queued_notifications(app):
    app.store.create_alert(demo_snapshot(), (123,), 300000)
    app.store.set("paused", True)
    task = asyncio.create_task(app.deliver())
    try:
        await asyncio.sleep(0.02)
        assert not app.telegram.calls
        assert app.store.db.execute("SELECT attempts FROM deliveries").fetchone()[0] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "text",
    [
        "/set volume nan",
        "/set volume inf",
        "/set volume -1",
        "/set share 2",
        "/set accounts 1.5",
        "/set volume",
        "/set bad 1",
        "/set cooldown 0",
    ],
)
async def test_invalid_configuration_does_not_take_effect(app, text):
    await app.handle_update(update(text))
    assert app.store.get("thresholds") is None


async def test_mute_toggle_persists_per_chat(app):
    await app.handle_update(callback("toggle:5:42"))
    assert app.store.get("muted:123") == [42]
    assert app.store.get("muted:-100", []) == []
    await app.handle_update(callback("toggle:5:42"))
    assert app.store.get("muted:123") == []


async def test_details_uses_saved_window_not_current_data(app):
    s = demo_snapshot()
    aid = app.store.create_alert(s, (123,), 300000)
    await app.handle_update(callback(f"detail:{aid}"))
    assert "393,654$" in app.telegram.calls[-1][1]["text"]
    assert "125" in app.telegram.calls[-1][1]["text"]
    await app.handle_update(callback("detail:999"))
    assert "удалены" in app.telegram.calls[-1][1]["text"]


@pytest.mark.parametrize(
    "command",
    [
        "/status",
        "/markets",
        "/stats OPENAI",
        "/stats BAD",
        "/settings",
        "/help",
        "/menu",
        "/top 15",
        "/test",
    ],
)
async def test_all_user_commands(app, command):
    await app.handle_update(update(command))
    assert app.telegram.calls[-1][0] == "sendMessage"
    text = app.telegram.calls[-1][1]["text"]
    assert text and len(text) < 4096
    if command == "/test":
        assert "ТЕСТ" in text


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(
    "error,status",
    [
        (None, "sent"),
        (TelegramError(403), "failed"),
        (TelegramError(429, 42), "pending"),
        (TimeoutError(), "pending"),
    ],
)
async def test_delivery_persistence_retry_after_and_permanent_errors(app, error, status):
    app.telegram.error = error
    aid = app.store.create_alert(demo_snapshot(), (123,), 300000)
    task = asyncio.create_task(app.deliver())
    try:
        await wait_until(
            lambda: (
                app.store.db.execute(
                    "SELECT attempts FROM deliveries WHERE alert_id=?", (aid,)
                ).fetchone()[0]
                == 1
            )
        )
        row = app.store.db.execute("SELECT * FROM deliveries").fetchone()
        assert row["status"] == status
        if isinstance(error, TelegramError) and error.code == 429:
            assert row["next_ms"] > now_ms() + 40000
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("case", ["expired", "muted", "removed_chat"])
async def test_old_muted_or_removed_destinations_are_not_sent(app, case):
    s = demo_snapshot()
    if case == "expired":
        s = replace(s, end_ms=now_ms() - 1000000)
    if case == "muted":
        app.store.set("muted:123", [42])
    app.store.create_alert(s, (999 if case == "removed_chat" else 123,), 300000)
    task = asyncio.create_task(app.deliver())
    try:
        await wait_until(
            lambda: app.store.db.execute("SELECT status FROM deliveries").fetchone()[0] != "pending"
        )
        assert not app.telegram.calls
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
