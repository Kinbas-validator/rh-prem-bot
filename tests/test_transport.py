import asyncio
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from rh_prem_bot.app import App
from rh_prem_bot.config import Settings
from rh_prem_bot.lighter import Feed, load_markets, now_ms
from rh_prem_bot.models import Market
from rh_prem_bot.telegram import Telegram, TelegramError


@asynccontextmanager
async def server(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def test_real_http_metadata_and_websocket_protocol(monkeypatch, store):
    app = web.Application()

    async def books(request):
        return web.json_response(
            {
                "code": 200,
                "order_books": [
                    {"symbol": "OPENAI", "market_id": 42, "market_type": "perp", "status": "active"}
                ],
            }
        )

    async def tokens(request):
        return web.json_response(
            {
                "code": 200,
                "tokens": [
                    {
                        "symbol": "OPENAI",
                        "asset_type": "RWA",
                        "market": "PERPS",
                        "categories": ["STOCK", "PRE_IPO"],
                    }
                ],
            }
        )

    app.router.add_get("/api/v1/orderBooks", books)
    app.router.add_get("/api/v1/tokenlist", tokens)
    received_pong = asyncio.Event()
    received_client_ping = asyncio.Event()

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "connected"})
        sub = await ws.receive_json()
        assert sub == {"type": "subscribe", "channel": "trade/42"}
        raw = {
            "trade_id_str": "12345678901234567890",
            "market_id": 42,
            "timestamp": now_ms(),
            "usd_amount": "123.456789",
            "ask_account_id": 10,
            "bid_account_id": 20,
            "is_maker_ask": True,
            "maker_fee": 120,
            "taker_fee": 100,
        }
        await ws.send_json({"type": "subscribed/trade", "channel": "trade:42", "trades": [raw]})
        await ws.send_json({"type": "ping"})
        assert await ws.receive_json() == {"type": "pong"}
        received_pong.set()
        await ws.send_json({"type": "update/trade", "channel": "trade:42", "trades": [raw, raw]})
        async for frame in ws:
            if frame.type == aiohttp.WSMsgType.TEXT and frame.json().get("type") == "ping":
                received_client_ping.set()
                await ws.send_json({"type": "pong"})
        return ws

    app.router.add_get("/stream", websocket)
    async with server(app) as base, aiohttp.ClientSession() as session:
        monkeypatch.setattr("rh_prem_bot.lighter.RH_API", base)
        monkeypatch.setattr("rh_prem_bot.lighter.KEEPALIVE_SECONDS", 0.01)
        monkeypatch.setattr("rh_prem_bot.lighter.RH_WS", base.replace("http:", "ws:") + "/stream")
        markets = await load_markets(session, Settings())
        inserted = asyncio.Event()
        feed = Feed(session, Settings(), store, markets, lambda trades: inserted.set())
        task = asyncio.create_task(feed.run())
        try:
            await asyncio.wait_for(received_pong.wait(), 2)
            await asyncio.wait_for(inserted.wait(), 2)
            await asyncio.wait_for(received_client_ping.wait(), 2)
            assert feed.inserted == 1
            t = store.trades(42, 0, now_ms() + 1000)[0]
            assert t.usd_micros == 123456789 and t.premium
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not feed.connected and not feed.observed_since


async def test_telegram_http_payload_rate_limit_and_token_redaction():
    app = web.Application()
    payloads = []

    async def api(request):
        payloads.append(await request.json())
        method = request.match_info["method"]
        if method == "limited":
            return web.json_response(
                {"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}, status=429
            )
        if method == "getWebhookInfo":
            return web.json_response({"ok": True, "result": {"url": "https://existing.invalid"}})
        if method == "editMessageText":
            return web.json_response(
                {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                }
            )
        return web.json_response({"ok": True, "result": {"username": "testbot"}})

    app.router.add_post("/botSECRET/{method}", api)
    async with server(app) as base, aiohttp.ClientSession() as session:
        tg = Telegram(session, "SECRET")
        tg._base = base + "/botSECRET/"
        await tg.send(123, "<b>Test</b>", {"inline_keyboard": []})
        assert payloads[-1]["parse_mode"] == "HTML"
        assert payloads[-1]["chat_id"] == 123
        assert payloads[-1]["link_preview_options"]["is_disabled"] is True
        with pytest.raises(TelegramError) as error:
            await tg.call("limited")
        assert error.value.retry_after == 12
        assert "SECRET" not in str(error.value)
        await tg.edit(123, 99, "same", {})
        with pytest.raises(ValueError, match="webhook"):
            await tg.check()


async def test_telegram_network_error_does_not_expose_secret():
    async with aiohttp.ClientSession() as session:
        tg = Telegram(session, "SUPER_SECRET")
        tg._base = "http://127.0.0.1:1/botSUPER_SECRET/"
        with pytest.raises(TelegramError) as error:
            await tg.send(123, "test")
        assert "SUPER_SECRET" not in str(error.value)
        assert error.value.code == 503


async def test_polling_checkpoints_offsets_and_fails_on_competing_poller(store):
    tg = Telegram(None, "SECRET")
    offsets, handled = [], []

    async def call(method, **payload):
        assert method == "getUpdates"
        offsets.append(payload["offset"])
        if len(offsets) == 1:
            return [{"update_id": 100}, {"update_id": 101}]
        raise TelegramError(409)

    async def handler(update):
        handled.append(update["update_id"])

    tg.call = call
    with pytest.raises(TelegramError) as error:
        await tg.poll(store, handler)
    assert error.value.code == 409
    assert offsets == [0, 102] and handled == [100, 101]
    assert store.get("telegram_offset") == 102


async def test_socket_close_reconnects_and_resubscribes(monkeypatch, store):
    app = web.Application()
    count = 0
    reconnected = asyncio.Event()

    async def stream(request):
        nonlocal count
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        assert (await ws.receive_json())["channel"] == "trade/42"
        count += 1
        await ws.send_json({"type": "subscribed/trade", "channel": "trade:42", "trades": []})
        if count == 1:
            await ws.close()
        else:
            reconnected.set()
            async for _ in ws:
                pass
        return ws

    app.router.add_get("/stream", stream)
    async with server(app) as base, aiohttp.ClientSession() as session:
        monkeypatch.setattr("rh_prem_bot.lighter.RH_WS", base.replace("http:", "ws:") + "/stream")
        monkeypatch.setattr("rh_prem_bot.lighter.random.random", lambda: 0)
        feed = Feed(session, Settings(), store, [Market(42, "OPENAI", "perp")])
        task = asyncio.create_task(feed.run())
        try:
            await asyncio.wait_for(reconnected.wait(), 4)
            assert count == 2 and feed.reconnects == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not feed.connected and not feed.observed_since


async def test_end_to_end_fill_spike_outbox_http_and_details(store, monkeypatch):
    end = now_ms()
    cfg = Settings(
        chat_ids=(123,),
        admin_ids=(123,),
        window_seconds=1,
        baseline_minutes=1,
        warmup_minutes=1,
        min_premium_usd=100,
        min_premium_accounts=2,
        evaluation_seconds=0.01,
    )
    feed = Feed(None, cfg, store, [Market(42, "OPENAI", "perp")])
    feed.connected = True
    feed.observed_since[42] = end - 61000
    raw = {
        "trade_id": 1,
        "market_id": 42,
        "timestamp": end - 60000,
        "usd_amount": "60",
        "ask_account_id": 10,
        "bid_account_id": 20,
        "is_maker_ask": True,
        "maker_fee": 120,
        "taker_fee": 350,
    }
    feed.process({"type": "update/trade", "channel": "trade:42", "trades": [raw]}, end - 60000)
    raw = {**raw, "trade_id": 2, "timestamp": end, "usd_amount": "1000"}
    feed.process({"type": "update/trade", "channel": "trade:42", "trades": [raw]}, end)
    monkeypatch.setattr("rh_prem_bot.app.now_ms", lambda: end)
    payloads = []
    received = asyncio.Event()
    webapp = web.Application()

    async def send(request):
        payloads.append(await request.json())
        received.set()
        return web.json_response({"ok": True, "result": {"message_id": 5}})

    webapp.router.add_post("/sendMessage", send)
    async with server(webapp) as base, aiohttp.ClientSession() as session:
        tg = Telegram(session, "TEST")
        tg._base = base + "/"
        app = App(cfg, store, feed, tg)
        tasks = [asyncio.create_task(app.evaluate()), asyncio.create_task(app.deliver())]
        try:
            await asyncio.wait_for(received.wait(), 2)
            assert "60,000$/мин" in payloads[0]["text"]
            assert "премиум-аккаунтов: 2" in payloads[0]["text"]
            button = payloads[0]["reply_markup"]["inline_keyboard"][0][0]
            assert button["text"] == "📈 Подробнее"
            aid = int(button["callback_data"].split(":")[1])
            assert store.alert(aid)["premium_usd"] == 1000
            assert store.alert(aid)["total_usd"] == 1000
            assert store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
