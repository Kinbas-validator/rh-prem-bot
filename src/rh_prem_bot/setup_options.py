"""Optional service installation prompts shared with the local setup wizard."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .config import Settings


async def installation_options(cfg: Settings) -> Settings:
    print("\nНастройки мониторинга. Enter оставляет значение в скобках.")
    while True:
        raw = (
            await asyncio.to_thread(
                input, "RWA: 1 — акции и pre-IPO, 2 — весь RWA (ETF и металлы тоже) [1]: "
            )
        ).strip() or "1"
        if raw in {"1", "2"}:
            cfg = replace(cfg, rwa_categories=("STOCK", "PRE_IPO") if raw == "1" else ())
            break
        print("Введите 1 или 2.")
    tickers = (
        await asyncio.to_thread(input, "Тикеры через запятую [Enter — все выбранные RWA]: ")
    ).strip()
    cfg = replace(cfg, markets=tuple(x.strip().upper() for x in tickers.split(",") if x.strip()))
    for attr, label, cast in (
        ("min_premium_usd", "Минимальный premium-объём за минутное окно, $", float),
        ("spike_multiplier", "Во сколько раз объём должен превышать средний", float),
        ("min_premium_accounts", "Минимум premium-аккаунтов", int),
        ("cooldown_seconds", "Пауза между сигналами одного тикера, секунд", int),
    ):
        while True:
            raw = (await asyncio.to_thread(input, f"{label} [{getattr(cfg, attr):g}]: ")).strip()
            try:
                value = cast(raw) if raw else getattr(cfg, attr)
                candidate = replace(cfg, **{attr: value})
                candidate.validate()
            except (ValueError, OverflowError) as exc:
                print(f"Некорректное значение: {exc}")
            else:
                cfg = candidate
                break
    return cfg


def update_env_template(content: str, values: dict[str, str]) -> str:
    lines = []
    remaining = dict(values)
    for line in content.splitlines():
        key = line.split("=", 1)[0]
        if not line.startswith("#") and key in remaining:
            line = f"{key}={remaining.pop(key)}"
        lines.append(line)
    lines.extend(f"{k}={v}" for k, v in remaining.items())
    return "\n".join(lines) + "\n"
