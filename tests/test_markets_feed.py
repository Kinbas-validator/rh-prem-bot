import pytest

from rh_prem_bot.config import Settings
from rh_prem_bot.lighter import Feed, select_markets
from rh_prem_bot.models import Market


def test_rwa_selection_uses_venue_categories_not_hardcoded_tickers():
    books = [
        {"symbol": s, "market_id": n, "market_type": k, "status": status}
        for n, s, k, status in [
            (1, "BTC", "perp", "active"),
            (42, "OPENAI", "perp", "active"),
            (99, "NEWIPO", "perp", "active"),
            (15, "NVDA", "perp", "inactive"),
            (2054, "NVDA/USDG", "spot", "active"),
            (25, "QQQ", "perp", "active"),
        ]
    ]
    tokens = [
        {"symbol": s, "asset_type": a, "market": k, "categories": c}
        for s, a, k, c in [
            ("BTC", "CRYPTO", "PERPS", ["STOCK"]),
            ("OPENAI", "RWA", "PERPS", ["STOCK", "PRE_IPO"]),
            ("NEWIPO", "RWA", "PERPS", ["PRE_IPO"]),
            ("NVDA", "RWA", "PERPS", ["STOCK"]),
            ("NVDA/USDG", "RWA", "SPOT", ["STOCK"]),
            ("QQQ", "RWA", "PERPS", ["ETF"]),
        ]
    ]
    assert [m.symbol for m in select_markets(books, tokens, Settings())] == ["NEWIPO", "OPENAI"]
    cfg = Settings(market_types=("spot",), markets=("NVDA/USDG",))
    assert select_markets(books, tokens, cfg)[0].id == 2054
    assert "QQQ" in [m.symbol for m in select_markets(books, tokens, Settings(rwa_categories=()))]
    with pytest.raises(ValueError):
        select_markets(books, tokens, Settings(markets=("BTC",)))
    with pytest.raises(ValueError):
        select_markets([], tokens, Settings())


@pytest.fixture
def feed(store):
    return Feed(None, Settings(), store, [Market(42, "OPENAI", "perp")])


def event(raws, kind="update/trade"):
    return {"type": kind, "channel": "trade:42", "trades": raws}


def raw_fill():
    return {
        "trade_id": 1,
        "market_id": 42,
        "timestamp": 1_000_000,
        "usd_amount": "10",
        "ask_account_id": 10,
        "bid_account_id": 20,
        "is_maker_ask": True,
        "maker_fee": 120,
        "type": "trade",
    }


def test_subscription_history_not_used_as_complete_backfill(feed, store):
    raw = raw_fill()
    feed.process(event([raw], "subscribed/trade"), 1_000_000)
    assert feed.observed_since[42] == 1_000_000
    assert store.trades(42, 0, 2_000_000) == []
    feed.process(event([raw, raw]), 1_000_001)
    assert feed.inserted == 1
    assert len(store.trades(42, 0, 2_000_000)) == 1


def test_gap_resets_warmup_and_invalid_events_do_not_crash(feed, store):
    feed.process(event([], "subscribed/trade"), 1)
    raw = raw_fill()
    feed.process(event([raw]), 1_040_000)
    assert feed.observed_since[42] == 1_040_000
    assert feed.inserted == 0
    raw["timestamp"] = 1_050_000
    feed.process(event([raw]), 1_040_000)
    assert feed.invalid == 1 and feed.inserted == 0
    feed.process(event([{}]), 1_060_000)
    assert feed.invalid == 2


def test_liquidations_and_unsubscribed_updates_are_ignored(feed):
    raw = raw_fill()
    feed.process(event([raw]), 1_000_000)
    assert feed.inserted == 0
    feed.process(event([], "subscribed/trade"), 1)
    raw["type"] = "liquidation"
    feed.process(event([raw]), 1_000_000)
    assert feed.inserted == 0 and feed.invalid == 0
