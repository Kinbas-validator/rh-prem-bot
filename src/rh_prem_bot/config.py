from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

RH_API = "https://api.rh.lighter.xyz"
RH_WS = "wss://api.rh.lighter.xyz/stream?readonly=true"


@dataclass
class Settings:
    token: str = field(default="", repr=False)
    chat_ids: tuple[int, ...] = ()
    admin_ids: tuple[int, ...] = ()
    markets: tuple[str, ...] = ()
    market_types: tuple[str, ...] = ("perp",)
    rwa_categories: tuple[str, ...] = ("STOCK", "PRE_IPO")
    window_seconds: int = 60
    baseline_minutes: int = 30
    warmup_minutes: int = 10
    spike_multiplier: float = 3.0
    min_premium_usd: float = 10000.0
    min_premium_share: float = 0.5
    min_premium_accounts: int = 2
    baseline_floor_usd: float = 1000.0
    cooldown_seconds: int = 300
    evaluation_seconds: float = 5.0
    flow_direction: str = "maker_taker"
    db_path: str = "data/bot.sqlite3"
    retention_days: int = 7
    alert_max_age_seconds: int = 900
    log_level: str = "INFO"

    def validate(self, telegram: bool = False) -> None:
        for name in (
            "window_seconds",
            "baseline_minutes",
            "warmup_minutes",
            "spike_multiplier",
            "min_premium_usd",
            "min_premium_accounts",
            "baseline_floor_usd",
            "cooldown_seconds",
            "evaluation_seconds",
            "retention_days",
            "alert_max_age_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} должен быть положительным конечным числом")
        if not 0 <= self.min_premium_share <= 1:
            raise ValueError("MIN_PREMIUM_SHARE должен быть от 0 до 1")
        if self.warmup_minutes > self.baseline_minutes:
            raise ValueError("WARMUP_MINUTES не должен превышать BASELINE_MINUTES")
        if self.baseline_minutes * 60 + self.window_seconds >= self.retention_days * 86400:
            raise ValueError("RETENTION_DAYS должен вмещать окно и базовый период")
        if self.flow_direction not in {"maker_taker", "seller_buyer"}:
            raise ValueError("FLOW_DIRECTION: maker_taker или seller_buyer")
        if not self.market_types or set(self.market_types) - {"perp", "spot"}:
            raise ValueError("MARKET_TYPES: perp, spot или perp,spot")
        if telegram and (not self.token or not self.chat_ids or not self.admin_ids):
            raise ValueError("Запустите ./start.sh setup: нужны токен, chat ID и admin ID")

    @classmethod
    def from_env(cls, path: str | Path = ".env") -> Settings:
        environment = {**dotenv_values(path), **os.environ}
        result = cls()
        for name in cls.__dataclass_fields__:
            key = (
                "TELEGRAM_BOT_TOKEN"
                if name == "token"
                else (
                    f"TELEGRAM_{name.upper()}"
                    if name in {"chat_ids", "admin_ids"}
                    else name.upper()
                )
            )
            raw = environment.get(key)
            if raw is None:
                continue
            if name in {"chat_ids", "admin_ids"}:
                value = tuple(int(x.strip()) for x in raw.split(",") if x.strip())
            elif name in {"markets", "market_types", "rwa_categories"}:
                value = tuple(
                    x.strip().lower() if name == "market_types" else x.strip().upper()
                    for x in raw.split(",")
                    if x.strip()
                )
            else:
                value = type(getattr(result, name))(raw)
            setattr(result, name, value)
        if not result.admin_ids:
            result.admin_ids = tuple(x for x in result.chat_ids if x > 0)
        result.validate()
        return result
