from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from dataclasses import replace

from .analytics import Snapshot, is_spike, snapshot
from .config import Settings
from .lighter import Feed, now_ms
from .messages import alert_text, detail_text, keyboard, menu_panel, ranking_panel
from .store import Store
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

EDITABLE = {
    "volume": ("min_premium_usd", float),
    "multiplier": ("spike_multiplier", float),
    "share": ("min_premium_share", float),
    "accounts": ("min_premium_accounts", int),
    "cooldown": ("cooldown_seconds", int),
}

HELP = (
    "<b>RWA Premium Monitor</b>\n"
    "Акции и pre-IPO на Lighter / Robinhood Chain.\n\n"
    "/start /top — таблица premium-объёма, 1/5/15/60м\n"
    "/menu — главное меню\n"
    "/status — соединение, прогрев, очередь\n"
    "/markets — отслеживаемые RWA\n"
    "/stats OPENAI — статистика за текущее окно\n"
    "/settings — пороги сигналов\n"
    "/reset — вернуть пороги из .env\n"
    "/set volume 50000 — минимальный premium-объём за окно\n"
    "/set multiplier 3 — минимальный xN\n"
    "/set share 0.5 — минимальная доля premium\n"
    "/set accounts 2 — минимум premium-аккаунтов\n"
    "/set cooldown 300 — пауза между сигналами по тикеру, секунды\n"
    "/pause /resume — выключить/включить сигналы\n"
    "/test — отправить помеченный тестовый сигнал\n"
    "/help — справка\n\n"
    "В направлениях по умолчанию слева maker, справа taker. "
    "Определение Premium — по ставке комиссии сделки; "
    "неизвестные ставки и Plus учитываются отдельно."
)


def demo_snapshot() -> Snapshot:
    return Snapshot(
        42,
        "OPENAI",
        now_ms(),
        60,
        395039,
        393654,
        393654,
        395039,
        72929,
        393654 / 72929,
        393654 / 395039,
        list(range(10001, 10010)),
        {"premium->standard": 982, "standard->premium": 385586, "premium->premium": 7086},
        [[10001, 150000], [10002, 100000]],
        125,
        0,
        1800,
        True,
        "maker_taker",
    )


class App:
    def __init__(self, cfg: Settings, store: Store, feed: Feed, telegram: Telegram | None):
        self.cfg, self.store, self.feed, self.telegram = cfg, store, feed, telegram
        self.env_cfg = replace(cfg)
        overrides = store.get("thresholds", {})
        candidate = replace(
            cfg, **{k: v for k, v in overrides.items() if k in {a for a, _ in EDITABLE.values()}}
        )
        candidate.validate()
        for attr, _ in EDITABLE.values():
            setattr(cfg, attr, getattr(candidate, attr))

    def current(self, market_id: int, end_ms: int | None = None) -> Snapshot:
        return snapshot(
            self.store,
            self.cfg,
            self.feed.markets[market_id],
            end_ms or now_ms(),
            self.feed.observed_since.get(market_id),
        )

    def rank_panel(self, chat_id: int, minutes: int):
        return ranking_panel(
            self.store,
            self.feed.markets.values(),
            minutes,
            now_ms(),
            set(self.store.get(f"muted:{chat_id}", [])),
            self.feed.observed_since,
        )

    async def evaluate(self):
        last_prune = 0
        while True:
            end = now_ms()
            if self.feed.connected and not self.store.get("paused", False):
                for mid in list(self.feed.markets):
                    if mid not in self.feed.observed_since:
                        continue
                    s = self.current(mid, end)
                    if is_spike(s, self.cfg):
                        aid = self.store.create_alert(
                            s,
                            self.cfg.chat_ids if self.telegram else (),
                            self.cfg.cooldown_seconds * 1000,
                        )
                        if aid:
                            log.info(
                                "ALERT %s premium=$%.0f x%.1f accounts=%d",
                                s.symbol,
                                s.premium_usd,
                                s.ratio,
                                len(s.premium_accounts),
                            )
                            if not self.telegram:
                                print(alert_text(s), flush=True)
            if end - last_prune > 3600000:
                self.store.prune(end - self.cfg.retention_days * 86400000)
                last_prune = end
            self.store.set(
                "health",
                {
                    "at_ms": end,
                    "pid": os.getpid(),
                    "connected": self.feed.connected,
                    "markets": len(self.feed.markets),
                    "subscribed": len(self.feed.observed_since),
                    "ready_markets": sum(
                        end - since
                        >= self.cfg.warmup_minutes * 60000 + self.cfg.window_seconds * 1000
                        for since in self.feed.observed_since.values()
                    ),
                    "inserted": self.feed.inserted,
                    "reconnects": self.feed.reconnects,
                    "invalid": self.feed.invalid,
                    "last_error": self.feed.last_error,
                },
            )
            await asyncio.sleep(self.cfg.evaluation_seconds)

    async def deliver(self):
        assert self.telegram is not None
        while True:
            if self.store.get("paused", False):
                await asyncio.sleep(1)
                continue
            for row in self.store.pending(now_ms()):
                aid, chat = row["alert_id"], row["chat_id"]
                saved = json.loads(row["payload"])
                if saved["market_id"] in self.store.get(f"muted:{chat}", []):
                    self.store.delivery(aid, chat, "muted")
                    continue
                if (
                    chat not in self.cfg.chat_ids
                    or now_ms() - row["created_ms"] > self.cfg.alert_max_age_seconds * 1000
                ):
                    self.store.delivery(aid, chat, "expired")
                    continue
                try:
                    await self.telegram.send(
                        chat, alert_text(Snapshot(**json.loads(row["payload"]))), keyboard(aid)
                    )
                except TelegramError as exc:
                    terminal = exc.code in {400, 403}
                    if exc.code == 401:
                        raise
                    delay = max(exc.retry_after, min(300, 2 ** min(row["attempts"] + 1, 8)))
                    self.store.delivery(
                        aid,
                        chat,
                        "failed" if terminal else "pending",
                        next_ms=now_ms() + delay * 1000,
                        error=str(exc),
                    )
                    log.warning(
                        "Alert %d delivery %s (code %d)",
                        aid,
                        "failed" if terminal else "retry scheduled",
                        exc.code,
                    )
                except TimeoutError:
                    self.store.delivery(
                        aid,
                        chat,
                        "pending",
                        next_ms=now_ms() + 10000,
                        error="Timeout (delivery outcome unknown)",
                    )
                else:
                    self.store.delivery(aid, chat, "sent")
                await asyncio.sleep(1.1)
            await asyncio.sleep(1)

    async def handle_update(self, update: dict):
        assert self.telegram is not None
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        chat = message.get("chat", {}).get("id")
        user = (callback or message).get("from", {}).get("id")
        if chat not in self.cfg.chat_ids:
            if callback:
                await self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback["id"],
                    text="Нет доступа",
                    show_alert=True,
                )
            return
        if callback:
            data = callback.get("data", "")
            if data.startswith(("rank:", "toggle:")):
                bits = data.split(":")
                if len(bits) not in {2, 3} or not all(x.isdigit() for x in bits[1:]):
                    return
                minutes = int(bits[1])
                if minutes not in {1, 5, 15, 60}:
                    return
                if bits[0] == "toggle":
                    if len(bits) != 3 or user not in self.cfg.admin_ids:
                        await self.telegram.call(
                            "answerCallbackQuery",
                            callback_query_id=callback["id"],
                            text="Только администратор",
                            show_alert=True,
                        )
                        return
                    mid = int(bits[2])
                    if mid not in self.feed.markets:
                        return
                    muted = set(self.store.get(f"muted:{chat}", []))
                    muted.symmetric_difference_update({mid})
                    self.store.set(f"muted:{chat}", sorted(muted))
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
                text, markup = self.rank_panel(chat, minutes)
                await self.telegram.edit(chat, message["message_id"], text, markup)
                return
            if data == "menu":
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
                text, markup = menu_panel()
                await self.telegram.edit(chat, message["message_id"], text, markup)
                return
            if data in {"settings", "status", "help"}:
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
                await self.handle_update(
                    {"message": {"chat": {"id": chat}, "from": {"id": user}, "text": f"/{data}"}}
                )
                return
            if data.startswith("detail:") and data[7:].isdigit():
                payload = self.store.alert(int(data[7:]))
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
                await self.telegram.send(
                    chat,
                    detail_text(Snapshot(**payload))
                    if payload
                    else "Данные сигнала удалены по сроку хранения.",
                )
            return
        parts = message.get("text", "").split()
        if not parts or not parts[0].startswith("/"):
            return
        command = parts[0].split("@", 1)[0].lower()
        if (
            command in {"/pause", "/resume", "/set", "/reset", "/test"}
            and user not in self.cfg.admin_ids
        ):
            await self.telegram.send(chat, "Эта команда доступна только администратору.")
            return
        if command in {"/start", "/top"}:
            minutes = int(parts[1]) if len(parts) > 1 and parts[1] in {"1", "5", "15", "60"} else 5
            text, markup = self.rank_panel(chat, minutes)
            await self.telegram.send(chat, text, markup)
            return
        if command == "/menu":
            text, markup = menu_panel()
            await self.telegram.send(chat, text, markup)
            return
        if command == "/help":
            text = HELP
        elif command == "/status":
            end = now_ms()
            ready = sum(
                end - since >= self.cfg.warmup_minutes * 60000 + self.cfg.window_seconds * 1000
                for since in self.feed.observed_since.values()
            )
            counts = self.store.db.execute(
                "SELECT status,COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
            queue = ", ".join(f"{r['status']}: {r['n']}" for r in counts) or "пуста"
            text = (
                f"<b>RWA Monitor</b>\nWebSocket: {'🟢' if self.feed.connected else '🔴'}\n"
                f"RWA-рынков: {len(self.feed.markets)}; подписок: "
                f"{len(self.feed.observed_since)}; готовы: {ready}\n"
                f"Сделок с запуска: {self.feed.inserted:,}; переподключений: "
                f"{self.feed.reconnects}; некорректных событий: {self.feed.invalid}\n"
                f"Сигналы: {'пауза' if self.store.get('paused', False) else 'включены'}\n"
                f"Доставка: {queue}\n"
                f"Прогрев: {self.cfg.warmup_minutes} мин + окно "
                f"{self.cfg.window_seconds}с после подписки/разрыва.\n"
                f"{html.escape(self.feed.last_error)}"
            )
        elif command == "/markets":
            text = "<b>RWA: акции и pre-IPO</b>\n" + " · ".join(
                html.escape(m.symbol) for m in self.feed.markets.values()
            )
        elif command == "/stats":
            symbol = parts[1].upper() if len(parts) > 1 else "OPENAI"
            market = next(
                (m for m in self.feed.markets.values() if m.symbol.upper() == symbol), None
            )
            text = (
                detail_text(self.current(market.id))
                if market
                else "Тикер не отслеживается. /markets"
            )
            if not self.feed.connected:
                text += "\n🔴 Нет соединения с биржей; данные могут быть неполными."
        elif command == "/settings":
            text = "<b>Пороги</b>\n" + "\n".join(
                f"{name}: {getattr(self.cfg, attr)}" for name, (attr, _) in EDITABLE.items()
            )
            text += (
                f"\nОкно {self.cfg.window_seconds}с; среднее до "
                f"{self.cfg.baseline_minutes} мин; направление {self.cfg.flow_direction}.\n"
                "Настройки /set сохраняются в SQLite и имеют приоритет над .env."
            )
        elif command == "/reset":
            for attr, _ in EDITABLE.values():
                setattr(self.cfg, attr, getattr(self.env_cfg, attr))
            self.store.set("thresholds", {})
            text = "Пороги восстановлены из .env. /settings"
        elif command == "/set":
            try:
                if len(parts) != 3 or parts[1] not in EDITABLE:
                    raise ValueError("Пример: /set volume 50000. Ключи: " + ", ".join(EDITABLE))
                attr, cast = EDITABLE[parts[1]]
                candidate = replace(self.cfg, **{attr: cast(parts[2])})
                candidate.validate()
                setattr(self.cfg, attr, getattr(candidate, attr))
                self.store.set(
                    "thresholds", {a: getattr(self.cfg, a) for a, _ in EDITABLE.values()}
                )
                text = f"Сохранено: {parts[1]} = {getattr(self.cfg, attr)}"
            except (ValueError, OverflowError) as exc:
                text = html.escape(str(exc))
        elif command in {"/pause", "/resume"}:
            self.store.set("paused", command == "/pause")
            text = (
                "Сигналы приостановлены; сбор данных продолжается."
                if command == "/pause"
                else ("Сигналы включены.")
            )
        elif command == "/test":
            await self.telegram.send(
                chat, "🧪 <b>ТЕСТ — синтетические данные</b>\n" + alert_text(demo_snapshot())
            )
            return
        else:
            text = "Неизвестная команда. /help"
        await self.telegram.send(chat, text)

    async def run(self):
        async with asyncio.TaskGroup() as group:
            group.create_task(self.feed.run())
            group.create_task(self.feed.refresh())
            group.create_task(self.evaluate())
            if self.telegram:
                group.create_task(self.deliver())
                group.create_task(self.telegram.poll(self.store, self.handle_update))
