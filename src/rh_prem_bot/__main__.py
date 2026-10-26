from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import json
import logging
import os
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from .app import App, demo_snapshot
from .config import RH_API, RH_WS, Settings
from .lighter import Feed, load_markets, now_ms
from .messages import alert_text, detail_text
from .models import FEE_SCHEDULE_VERSION
from .setup_options import installation_options, update_env_template
from .store import Store
from .telegram import Telegram, TelegramError


@contextmanager
def instance_lock(db_path: str):
    path = Path(db_path + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("С этим DB_PATH уже запущен другой экземпляр бота") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def setup(cfg: Settings, path: Path, *, advanced: bool = False, service_mode: bool = False):
    if await asyncio.to_thread(path.exists):
        raise ValueError(f"{path} уже существует. Отредактируйте его вручную; файл не перезаписан.")
    token = (await asyncio.to_thread(getpass.getpass, "Токен от @BotFather (ввод скрыт): ")).strip()
    if not token or any(c.isspace() for c in token) or ":" not in token:
        raise ValueError("Некорректный формат токена")
    async with aiohttp.ClientSession() as session:
        telegram = Telegram(session, token)
        me = await telegram.check()
        print(f"Бот @{me['username']}. Откройте его в Telegram и отправьте /start.")
        raw = (
            await asyncio.to_thread(
                input, "Числовой chat ID (Enter — найти личный чат по /start): "
            )
        ).strip()
        if raw:
            chat_id = int(raw)
        else:
            print("Ожидаю /start в личном чате (до 3 минут)...")
            deadline = now_ms() + 180000
            candidates = {}
            offset = 0
            while now_ms() < deadline:
                updates = await telegram.call(
                    "getUpdates", offset=offset, timeout=25, allowed_updates=["message"]
                )
                for update in updates:
                    offset = max(offset, update["update_id"] + 1)
                    m = update.get("message", {})
                    c = m.get("chat", {})
                    if c.get("type") == "private" and m.get("text", "").startswith("/start"):
                        candidates[c["id"]] = c
                if candidates:
                    break
            if len(candidates) != 1:
                raise ValueError("Не найден один личный чат. Повторите setup с числовым chat ID.")
            chat_id = next(iter(candidates))
            name = candidates[chat_id].get("first_name", "")
            print(f"Найден чат: {name} (ID {chat_id}).")
        if chat_id == 0:
            raise ValueError("Chat ID не может быть 0")
        admin_id = (
            chat_id
            if chat_id > 0
            else int((await asyncio.to_thread(input, "Ваш личный Telegram user ID: ")).strip())
        )
        if admin_id <= 0:
            raise ValueError("Admin ID должен быть положительным")
        if advanced:
            cfg = await installation_options(cfg)
        if service_mode:
            cfg.db_path = "/var/lib/rh-prem-bot/bot.sqlite3"
        cfg.validate()
        # Verify that this exact destination is reachable before saving credentials.
        message = (
            "✅ Настройки приняты. Установщик проверяет и запускает сервис."
            if service_mode
            else "✅ RWA Premium Monitor настроен. Запустите ./start.sh run"
        )
        await telegram.send(chat_id, message)
        content = await asyncio.to_thread(Path(".env.example").read_text)
        content = update_env_template(
            content,
            {
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_CHAT_IDS": str(chat_id),
                "TELEGRAM_ADMIN_IDS": str(admin_id),
                "MARKETS": ",".join(cfg.markets),
                "RWA_CATEGORIES": ",".join(cfg.rwa_categories),
                "MIN_PREMIUM_USD": str(cfg.min_premium_usd),
                "SPIKE_MULTIPLIER": str(cfg.spike_multiplier),
                "MIN_PREMIUM_ACCOUNTS": str(cfg.min_premium_accounts),
                "COOLDOWN_SECONDS": str(cfg.cooldown_seconds),
                "DB_PATH": cfg.db_path,
            },
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        if service_mode:
            print("Настройки сохранены. Продолжаю установку сервиса.")
        else:
            print(f"Сохранено в {path} (доступ только владельцу). Запуск: ./start.sh run")


async def doctor(cfg: Settings):
    async with aiohttp.ClientSession() as session:
        markets = await load_markets(session, cfg)
        sample = next((m for m in markets if m.symbol == "OPENAI"), markets[0])
        print(f"REST: OK; RWA-рынков: {len(markets)}")
        print(", ".join(m.symbol for m in markets))
        async with session.ws_connect(RH_WS, heartbeat=20) as ws:
            await ws.send_json({"type": "subscribe", "channel": f"trade/{sample.id}"})
            async with asyncio.timeout(20):
                async for frame in ws:
                    if frame.type != aiohttp.WSMsgType.TEXT:
                        continue
                    message = frame.json()
                    if message.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                    if message.get("type") == "subscribed/trade":
                        print(f"WebSocket: OK; trade/{sample.id} ({sample.symbol})")
                        break
                else:
                    raise RuntimeError("WebSocket closed before subscription acknowledgement")
        if cfg.token:
            telegram = Telegram(session, cfg.token)
            me = await telegram.check()
            print(f"Telegram getMe: OK (@{me['username']}); сообщений не отправлено")
        else:
            print("Telegram: токен не задан; доставка не проверялась")
        print(f"Тарифная модель: {FEE_SCHEDULE_VERSION}; источник {RH_API}")


async def monitor(cfg: Settings, seconds: int | None, report: str | None, live: bool):
    if live:
        cfg.validate(telegram=True)
    stats = Counter()
    examples = []

    def on_batch(trades):
        for t in trades:
            stats[f"maker:{t.maker_tier}"] += 1
            stats[f"taker:{t.taker_tier}"] += 1
            stats["premium_fills"] += int(t.premium)
            if len(examples) < 6:
                examples.append(asdict(t))

    started = now_ms()
    with instance_lock(cfg.db_path):
        store = Store(cfg.db_path)
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "rh-prem-bot/1.0"}) as session:
                markets = await load_markets(session, cfg)
                tg = Telegram(session, cfg.token) if live else None
                if tg:
                    await tg.check()
                feed = Feed(session, cfg, store, markets, on_batch)
                app = App(cfg, store, feed, tg)
                logging.info("Monitoring RWA: %s", ", ".join(m.symbol for m in markets))
                try:
                    async with asyncio.timeout(seconds):
                        await app.run()
                except TimeoutError:
                    if seconds is None:
                        raise
                if report:
                    end = now_ms()
                    payload = {
                        "started_utc": datetime.fromtimestamp(started / 1000, UTC).isoformat(),
                        "duration_seconds": (end - started) / 1000,
                        "source": RH_API,
                        "fee_schedule": FEE_SCHEDULE_VERSION,
                        "configuration": {
                            name: getattr(cfg, name)
                            for name in (
                                "markets",
                                "market_types",
                                "rwa_categories",
                                "window_seconds",
                                "baseline_minutes",
                                "warmup_minutes",
                                "spike_multiplier",
                                "min_premium_usd",
                                "min_premium_share",
                                "min_premium_accounts",
                                "baseline_floor_usd",
                                "cooldown_seconds",
                                "flow_direction",
                            )
                        },
                        "telegram_delivery_tested": False,
                        "markets": [asdict(m) for m in feed.markets.values()],
                        "unique_fills_inserted": feed.inserted,
                        "fee_observations": dict(stats),
                        "invalid_events": feed.invalid,
                        "reconnects": feed.reconnects,
                        "samples": examples,
                        "last_health": store.get("health"),
                        "alerts_created": store.db.execute(
                            "SELECT COUNT(*) FROM alerts"
                        ).fetchone()[0],
                        "alert_samples": [
                            json.loads(row[0])
                            for row in store.db.execute(
                                "SELECT payload FROM alerts ORDER BY id LIMIT 3"
                            )
                        ],
                    }
                    path = Path(report)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(
                        path.write_text, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                    )
                    print(f"Отчёт: {path}; уникальных сделок: {feed.inserted}")
                    if feed.inserted == 0:
                        raise RuntimeError(
                            "За время проверки не получены сделки; проверка неполная"
                        )
        finally:
            h = store.get("health")
            if h:
                store.set("health", {**h, "at_ms": now_ms(), "connected": False, "stopped": True})
            store.close()


def health(cfg: Settings) -> int:
    if not Path(cfg.db_path).exists():
        print("База данных ещё не создана")
        return 1
    store = Store(cfg.db_path)
    try:
        h = store.get("health", {})
        healthy = (
            now_ms() - h.get("at_ms", 0) < max(60000, cfg.evaluation_seconds * 3000)
            and h.get("connected")
            and h.get("markets", 0) > 0
            and h.get("subscribed") == h.get("markets")
        )
        print(json.dumps({"healthy": bool(healthy), **h}, ensure_ascii=False, indent=2))
        return 0 if healthy else 1
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="RWA Premium Monitor — Lighter / Robinhood Chain")
    parser.add_argument("--env-file", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)
    wizard = sub.add_parser("setup", help="Настроить Telegram и создать .env")
    wizard.add_argument("--advanced", action="store_true", help="Также запросить пороги и RWA")
    wizard.add_argument("--service-mode", action="store_true", help="Настройки для install.sh")
    sub.add_parser("run", help="Мониторинг и Telegram")
    mon = sub.add_parser("monitor", help="Живой сбор без Telegram")
    mon.add_argument("--seconds", type=int)
    mon.add_argument("--report")
    sub.add_parser("doctor", help="Проверить REST, WS и Telegram getMe")
    sub.add_parser("health", help="Проверить здоровье запущенного процесса")
    sub.add_parser("demo", help="Показать пример сигнала без сети")
    smoke = sub.add_parser("smoke", help="Изолированная проверка на реальной бирже без Telegram")
    smoke.add_argument("--seconds", type=int, default=60)
    smoke.add_argument("--report", default="data/live-smoke.json")
    args = parser.parse_args()
    try:
        cfg = Settings.from_env(args.env_file)
        logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(message)s")
        if getattr(args, "seconds", None) is not None and args.seconds <= 0:
            raise ValueError("--seconds должен быть положительным")
        if args.command == "health":
            sys.exit(health(cfg))
        if args.command == "demo":
            print("ТЕСТ — синтетический пример\n" + alert_text(demo_snapshot()))
            print("\n" + detail_text(demo_snapshot()))
            return
        if args.command == "setup":
            asyncio.run(
                setup(
                    cfg, Path(args.env_file), advanced=args.advanced, service_mode=args.service_mode
                )
            )
        elif args.command == "doctor":
            asyncio.run(doctor(cfg))
        elif args.command == "smoke":
            with tempfile.TemporaryDirectory(prefix="rh-prem-smoke-") as temp:
                cfg.db_path = str(Path(temp) / "smoke.sqlite3")
                asyncio.run(monitor(cfg, args.seconds, args.report, False))
        else:
            asyncio.run(
                monitor(
                    cfg,
                    getattr(args, "seconds", None),
                    getattr(args, "report", None),
                    args.command == "run",
                )
            )
    except KeyboardInterrupt:
        print("\nОстановлено; данные сохранены.")
    except Exception as exc:
        # Avoid network exception URLs (which may include tokens).
        errors = exc.exceptions if isinstance(exc, ExceptionGroup) else (exc,)
        for error in errors:
            if isinstance(error, (ValueError, RuntimeError, TelegramError)):
                print(f"Ошибка: {error}", file=sys.stderr)
            else:
                print(
                    f"Ошибка: {type(error).__name__}. Проверьте конфигурацию и сеть.",
                    file=sys.stderr,
                )
        sys.exit(1)


if __name__ == "__main__":
    main()
