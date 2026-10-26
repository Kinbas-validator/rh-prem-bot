from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class Tier(StrEnum):
    STANDARD = "standard"
    PREMIUM = "premium"
    PLUS = "plus"
    UNKNOWN = "unknown"


# RH fee ticks = percentage × 10,000 (0.0120% → 120).
# Verified 2026-10-20 against RH docs, not Lighter Core's different fee schedule.
PREMIUM_MAKER = frozenset({84, 96, 102, 108, 114, 117, 120})
PREMIUM_TAKER = frozenset({245, 280, 298, 315, 333, 341, 350})
FEE_SCHEDULE_VERSION = "rh-2026-10-20"


def classify_fee(value: Any, *, maker: bool) -> Tier:
    # Absent zero fields are omitted by Lighter. Explicit null/malformed ≠ zero.
    if isinstance(value, bool) or not isinstance(value, int):
        return Tier.UNKNOWN
    if value == 0 or (not maker and value == 100):
        return Tier.STANDARD
    if value == 150:
        return Tier.PLUS
    if value in (PREMIUM_MAKER if maker else PREMIUM_TAKER):
        return Tier.PREMIUM
    return Tier.UNKNOWN


def integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Invalid {label}")
    result = int(value)
    if result < 0:
        raise ValueError(f"Negative {label}")
    return result


@dataclass(frozen=True)
class Market:
    id: int
    symbol: str
    kind: str


@dataclass(frozen=True)
class Trade:
    id: str
    market_id: int
    timestamp_ms: int
    usd_micros: int
    maker_account: int
    taker_account: int
    maker_tier: Tier
    taker_tier: Tier
    is_maker_ask: bool
    maker_fee: int | None
    taker_fee: int | None

    @property
    def premium(self) -> bool:
        return Tier.PREMIUM in (self.maker_tier, self.taker_tier)

    def flow(self, direction: str) -> tuple[Tier, Tier]:
        if direction == "seller_buyer" and not self.is_maker_ask:
            return self.taker_tier, self.maker_tier
        return self.maker_tier, self.taker_tier

    @classmethod
    def parse(cls, raw: dict) -> Trade:
        if raw.get("type", "trade") != "trade":
            raise ValueError("Only regular fills are counted")
        side = raw["is_maker_ask"]
        if not isinstance(side, bool):
            raise ValueError("is_maker_ask must be boolean")
        try:
            # usd_amount accounts for contract multipliers; never substitute size × price.
            amount = Decimal(str(raw["usd_amount"]))
        except (InvalidOperation, KeyError) as exc:
            raise ValueError("Invalid usd_amount") from exc
        if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000000"):
            raise ValueError("Invalid usd_amount")
        ask = integer(raw["ask_account_id"], "ask_account_id")
        bid = integer(raw["bid_account_id"], "bid_account_id")
        mf, tf = raw.get("maker_fee", 0), raw.get("taker_fee", 0)
        return cls(
            id=str(integer(raw.get("trade_id_str", raw.get("trade_id")), "trade_id")),
            market_id=integer(raw["market_id"], "market_id"),
            timestamp_ms=integer(raw["timestamp"], "timestamp"),
            usd_micros=int(amount * 1_000_000),
            maker_account=ask if side else bid,
            taker_account=bid if side else ask,
            maker_tier=classify_fee(mf, maker=True),
            taker_tier=classify_fee(tf, maker=False),
            is_maker_ask=side,
            maker_fee=mf if isinstance(mf, int) else None,
            taker_fee=tf if isinstance(tf, int) else None,
        )
