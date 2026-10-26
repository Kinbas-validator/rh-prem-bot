from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .config import Settings
from .models import Market, Tier
from .store import Store


@dataclass
class Snapshot:
    market_id: int
    symbol: str
    end_ms: int
    window_seconds: int
    total_usd: float
    premium_usd: float
    premium_per_minute: float
    total_per_minute: float
    baseline_per_minute: float
    ratio: float
    share: float
    premium_accounts: list[int]
    flows: dict[str, float]
    account_volumes: list[list]
    trade_count: int
    unknown_usd: float
    baseline_seconds: float
    ready: bool
    flow_direction: str


def snapshot(
    store: Store, cfg: Settings, market: Market, end_ms: int, observed_since_ms: int | None
) -> Snapshot:
    start = end_ms - cfg.window_seconds * 1000
    baseline_start = max(start - cfg.baseline_minutes * 60000, observed_since_ms or start)
    baseline_seconds = max(0, (start - baseline_start) / 1000)
    rows = store.trades(market.id, start, end_ms)
    total = premium = unknown = 0
    flows: Counter = Counter()
    accounts: Counter = Counter()
    for t in rows:
        total += t.usd_micros
        if Tier.UNKNOWN in (t.maker_tier, t.taker_tier):
            unknown += t.usd_micros
        if t.premium:
            premium += t.usd_micros
            left, right = t.flow(cfg.flow_direction)
            flows[f"{left}->{right}"] += t.usd_micros
        # Distinct account indices, with per-account participation (may sum to > volume).
        ids = set()
        if t.maker_tier == Tier.PREMIUM:
            ids.add(t.maker_account)
        if t.taker_tier == Tier.PREMIUM:
            ids.add(t.taker_account)
        for account_id in ids:
            accounts[account_id] += t.usd_micros
    base = (
        (store.premium_sum(market.id, baseline_start, start) / 1e6 * 60 / baseline_seconds)
        if baseline_seconds
        else 0
    )
    ppm = premium / 1e6 * 60 / cfg.window_seconds
    return Snapshot(
        market.id,
        market.symbol,
        end_ms,
        cfg.window_seconds,
        total / 1e6,
        premium / 1e6,
        ppm,
        total / 1e6 * 60 / cfg.window_seconds,
        base,
        ppm / max(base, cfg.baseline_floor_usd),
        premium / total if total else 0,
        sorted(accounts),
        {k: v / 1e6 for k, v in flows.items()},
        [[k, v / 1e6] for k, v in accounts.most_common(15)],
        len(rows),
        unknown / 1e6,
        baseline_seconds,
        baseline_seconds >= cfg.warmup_minutes * 60,
        cfg.flow_direction,
    )


def is_spike(s: Snapshot, cfg: Settings) -> bool:
    return (
        s.ready
        and s.premium_usd >= cfg.min_premium_usd
        and s.share >= cfg.min_premium_share
        and len(s.premium_accounts) >= cfg.min_premium_accounts
        and s.ratio >= cfg.spike_multiplier
    )
