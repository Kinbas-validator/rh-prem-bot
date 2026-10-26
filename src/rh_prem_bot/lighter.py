from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable

import aiohttp

from .config import RH_API, RH_WS, Settings
from .models import Market, Trade
from .store import Store

log = logging.getLogger(__name__)
KEEPALIVE_SECONDS = 30


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def select_markets(books: list[dict], tokens: list[dict], cfg: Settings) -> list[Market]:
    eligible = {
        (t["symbol"].upper(), "perp" if t.get("market") == "PERPS" else "spot")
        for t in tokens
        if t.get("asset_type") == "RWA"
        and t.get("market") in {"PERPS", "SPOT"}
        and (not cfg.rwa_categories or set(t.get("categories", [])) & set(cfg.rwa_categories))
    }
    result = sorted(
        [
            Market(int(b["market_id"]), b["symbol"], b["market_type"])
            for b in books
            if b.get("status") == "active"
            and b.get("market_type") in cfg.market_types
            and (b["symbol"].upper(), b["market_type"]) in eligible
            and (not cfg.markets or b["symbol"].upper() in cfg.markets)
        ],
        key=lambda m: m.symbol,
    )
    missing = set(cfg.markets) - {m.symbol.upper() for m in result}
    if missing:
        raise ValueError(f"Не найдены активные RWA с выбранными категориями: {', '.join(missing)}")
    if not result:
        raise ValueError("API не вернул подходящие RWA-рынки. Крипторынки не подставляются.")
    if len(result) > 450:
        raise ValueError("Слишком много рынков для одного соединения; уточните фильтры")
    return result


async def get_json(session: aiohttp.ClientSession, endpoint: str) -> dict:
    # Small public request budget: metadata only, no per-account REST calls.
    for attempt in range(4):
        async with session.get(
            RH_API + "/api/v1/" + endpoint, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            if response.status == 429 or response.status >= 500:
                delay = min(60, float(response.headers.get("Retry-After", 2 ** (attempt + 1))))
            else:
                response.raise_for_status()
                data = await response.json()
                if data.get("code", 200) != 200:
                    raise RuntimeError(f"Lighter API error {data.get('code')}")
                return data
        await asyncio.sleep(delay)
    raise RuntimeError(f"Lighter API unavailable: {endpoint}")


async def load_markets(session: aiohttp.ClientSession, cfg: Settings) -> list[Market]:
    books = await get_json(session, "orderBooks")
    tokens = await get_json(session, "tokenlist")
    return select_markets(books["order_books"], tokens["tokens"], cfg)


class Feed:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        cfg: Settings,
        store: Store,
        markets: list[Market],
        on_batch: Callable | None = None,
    ):
        self.session, self.cfg, self.store = session, cfg, store
        self.markets = {m.id: m for m in markets}
        self.observed_since: dict[int, int] = {}
        self.connected = False
        self.last_message_ms = 0
        self.inserted = 0
        self.reconnects = 0
        self.invalid = 0
        self.last_error = ""
        self.on_batch = on_batch
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()

    async def subscribe(self, market_id: int, *, remove: bool = False):
        async with self._send_lock:
            if self.ws and not self.ws.closed:
                await self.ws.send_json(
                    {
                        "type": "unsubscribe" if remove else "subscribe",
                        "channel": f"trade/{market_id}",
                    }
                )
                # Includes refreshes and keeps the 200 client messages/minute budget.
                await asyncio.sleep(0.4)

    async def refresh(self):
        while True:
            await asyncio.sleep(900)
            try:
                new = {m.id: m for m in await load_markets(self.session, self.cfg)}
                old = self.markets
                self.markets = new
                for mid in old.keys() - new.keys():
                    self.observed_since.pop(mid, None)
                    await self.subscribe(mid, remove=True)
                for mid in new.keys() - old.keys():
                    await self.subscribe(mid)
                log.info("RWA metadata refreshed: %d markets", len(new))
            except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, KeyError) as exc:
                log.warning(
                    "Market refresh failed (%s); keeping last market list", type(exc).__name__
                )

    def process(self, message: dict, received_ms: int):
        kind = message.get("type", "")
        if kind not in {"subscribed/trade", "update/trade"}:
            return
        mid = int(message["channel"].replace("/", ":").split(":")[-1])
        if mid not in self.markets:
            return
        if kind == "subscribed/trade":
            # Subscription includes historical fills and old liquidations. Not a backfill.
            self.observed_since[mid] = received_ms
            return
        if mid not in self.observed_since:
            return
        trades = []
        for raw in message.get("trades", []):
            if raw.get("type", "trade") != "trade":
                continue
            try:
                t = Trade.parse(raw)
                if t.market_id != mid or t.timestamp_ms > received_ms + 5000:
                    raise ValueError("Wrong market or future timestamp")
                if t.timestamp_ms < received_ms - 30000:
                    # A delayed stream cannot provide a complete realtime window.
                    self.observed_since[mid] = received_ms
                    self.last_error = f"Задержка сделок >30с: {self.markets[mid].symbol}"
                    continue
                trades.append(t)
            except (ValueError, KeyError, TypeError):
                self.invalid += 1
                self.observed_since[mid] = received_ms
                self.last_error = f"Некорректное событие: {self.markets[mid].symbol}"
                log.warning("Rejected malformed fill in market %s", mid)
        self.inserted += self.store.add_trades(trades)
        if self.on_batch and trades:
            self.on_batch(trades)

    async def run(self):
        backoff = 1
        while True:
            self.observed_since.clear()
            started = time.monotonic()
            try:
                async with self.session.ws_connect(
                    RH_WS,
                    heartbeat=20,
                    max_msg_size=8 * 1024 * 1024,
                    timeout=aiohttp.ClientWSTimeout(ws_close=5, ws_receive=65),
                ) as ws:
                    self.ws, self.connected = ws, True
                    self.last_error = ""
                    log.info(
                        "Connected to RH trade stream; subscribing to %d RWA markets",
                        len(self.markets),
                    )

                    async def subscribe_all():
                        for mid in list(self.markets):
                            await self.subscribe(mid)

                    async def keepalive():
                        # aiohttp heartbeat is reset by incoming trades. Lighter requires
                        # outgoing frames even on a busy stream, so use a separate timer.
                        while True:
                            await asyncio.sleep(KEEPALIVE_SECONDS)
                            await ws.send_json({"type": "ping"})

                    subscriber = asyncio.create_task(subscribe_all())
                    keeper = asyncio.create_task(keepalive())
                    try:
                        async for frame in ws:
                            if frame.type == aiohttp.WSMsgType.TEXT:
                                self.last_message_ms = now_ms()
                                msg = json.loads(frame.data)
                                kind = msg.get("type")
                                if kind == "ping":
                                    await ws.send_json({"type": "pong"})
                                elif kind in {"shutdown", "error"} or "error" in msg:
                                    raise RuntimeError(f"Lighter stream {kind}")
                                else:
                                    self.process(msg, self.last_message_ms)
                            elif frame.type == aiohttp.WSMsgType.ERROR:
                                raise RuntimeError("WebSocket transport error")
                    finally:
                        subscriber.cancel()
                        keeper.cancel()
                        await asyncio.gather(subscriber, keeper, return_exceptions=True)
            except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, KeyError) as exc:
                self.last_error = f"Соединение прервано: {type(exc).__name__}"
                log.warning("RH stream disconnected (%s); reconnecting", type(exc).__name__)
            finally:
                self.connected, self.ws = False, None
                self.observed_since.clear()
            self.reconnects += 1
            if time.monotonic() - started > 60:
                backoff = 1
            await asyncio.sleep(backoff + random.random())
            backoff = min(60, backoff * 2)
