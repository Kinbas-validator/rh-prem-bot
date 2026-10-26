from dataclasses import replace

import pytest

from rh_prem_bot.analytics import is_spike, snapshot
from rh_prem_bot.app import demo_snapshot
from rh_prem_bot.config import Settings
from rh_prem_bot.messages import alert_text, detail_text, ranking_panel
from rh_prem_bot.models import Market
from rh_prem_bot.store import Store

MARKET = Market(42, "OPENAI", "perp")


def test_volume_not_doubled_distinct_accounts_and_flows(store, fill):
    store.add_trades(
        [
            fill(trade_id=1, usd_amount="100"),
            fill(trade_id=2, usd_amount="20", maker_fee=0),
            fill(trade_id=3, usd_amount="30", taker_fee=0),
            fill(trade_id=4, usd_amount="40", maker_fee=0, taker_fee=0),
            fill(trade_id=5, usd_amount="50", maker_fee=150),
            fill(trade_id=6, usd_amount="60", maker_fee=999),
        ]
    )
    s = snapshot(store, Settings(), MARKET, 1_000_000, 1000)
    assert s.total_usd == 300
    assert s.premium_usd == 260
    assert s.premium_accounts == [101, 202]
    assert s.flows == {
        "premium->premium": 100,
        "standard->premium": 20,
        "premium->standard": 30,
        "plus->premium": 50,
        "unknown->premium": 60,
    }
    assert sum(s.flows.values()) == s.premium_usd
    assert s.unknown_usd == 60


def test_half_open_windows_and_baseline_excludes_current(store, fill):
    end = 3_000_000
    cfg = Settings(baseline_minutes=30, warmup_minutes=10, baseline_floor_usd=1)
    start = end - 60000
    store.add_trades(
        [
            fill(trade_id=1, timestamp=start - 60000, usd_amount="600"),
            fill(trade_id=2, timestamp=start, usd_amount="400"),
            fill(trade_id=3, timestamp=start + 1, usd_amount="6000"),
            fill(trade_id=4, timestamp=end, usd_amount="4000"),
            fill(trade_id=5, timestamp=end + 1, usd_amount="100000"),
        ]
    )
    s = snapshot(store, cfg, MARKET, end, start - 600000)
    assert s.baseline_per_minute == 100
    assert s.premium_per_minute == 10000
    assert s.ratio == 100 and s.ready
    assert s.trade_count == 2


def test_zero_minutes_in_baseline_not_just_active_minutes(store, fill):
    cfg = Settings(baseline_floor_usd=1)
    store.add_trades([fill(timestamp=600000, usd_amount="600")])
    s = snapshot(store, cfg, MARKET, 1260000, 60000)
    assert s.baseline_per_minute == pytest.approx(600 / 19)


def test_warmup_gap_and_zero_baseline_floor(store, fill):
    cfg = Settings(min_premium_usd=1, min_premium_accounts=1)
    store.add_trades([fill(timestamp=1_000_000, usd_amount="10000")])
    s = snapshot(store, cfg, MARKET, 1_000_000, 999000)
    assert not s.ready and not is_spike(s, cfg)
    s = snapshot(store, cfg, MARKET, 1_000_000, 1000)
    assert s.ready and s.baseline_per_minute == 0 and s.ratio == 10
    assert is_spike(s, cfg)


@pytest.mark.parametrize(
    "changes",
    [
        {"min_premium_usd": 1_000_000},
        {"spike_multiplier": 100},
        {"min_premium_share": 1},
        {"min_premium_accounts": 100},
    ],
)
def test_all_thresholds_gate_signals(changes):
    assert not is_spike(demo_snapshot(), replace(Settings(), **changes))


def test_dedup_and_durable_cooldown_outbox(tmp_path, fill):
    path = str(tmp_path / "bot.sqlite3")
    store = Store(path)
    t = fill()
    assert store.add_trades([t, t]) == 1
    s = demo_snapshot()
    aid = store.create_alert(s, (12, 13), 300000)
    assert aid and len(store.pending(s.end_ms)) == 2
    store.close()
    store = Store(path)
    try:
        assert store.add_trades([t]) == 0
        assert store.create_alert(s, (12,), 300000) is None
        assert store.alert(aid)["premium_usd"] == s.premium_usd
        store.delivery(aid, 12, "sent")
        assert [r["chat_id"] for r in store.pending(s.end_ms)] == [13]
        assert store.create_alert(replace(s, end_ms=s.end_ms + 300000), (12,), 300000)
        store.prune(s.end_ms + 300001)
        assert not store.pending(s.end_ms + 300001)
        assert store.alert(aid) is None
    finally:
        store.close()


def test_screenshot_alert_format_and_html_safety():
    text = alert_text(demo_snapshot())
    assert "393,654$/мин" in text
    assert "среднее 72,929$/мин, x5.4" in text
    assert "stand→prem 385,586$" in text
    assert "премиум-аккаунтов: 9" in text
    assert "&lt;x&gt;" in alert_text(replace(demo_snapshot(), symbol="<x>"))
    assert len(detail_text(demo_snapshot())) < 4096


def test_second_screenshot_ranking_and_period_buttons(store, fill):
    store.add_trades(
        [
            fill(timestamp=1_000_000, usd_amount="50000"),
            fill(trade_id=2, market_id=15, timestamp=1_000_000, usd_amount="25000"),
            fill(trade_id=3, timestamp=600000, usd_amount="9999999"),
        ]
    )
    markets = [MARKET, Market(15, "NVDA", "perp")]
    text, markup = ranking_panel(store, markets, 5, 1_000_000, {15}, {42: 1, 15: 1})
    assert "Token" in text and "Prem" in text and "Vol" in text
    assert "10k" in text and "5k" in text
    assert text.index("OPENAI") < text.index("NVDA")
    buttons = markup["inline_keyboard"]
    assert [b["text"] for b in buttons[0]] == ["1м", "• 5м", "15м", "60м", "⟳"]
    assert buttons[1][0]["text"] == "🔊 OPENAI"
    assert buttons[1][1]["text"] == "NVDA"


def test_same_premium_account_counted_once_even_on_both_sides(store, fill):
    store.add_trades([fill(ask_account_id=10, bid_account_id=10, usd_amount="100")])
    s = snapshot(store, Settings(), MARKET, 1_000_000, 1)
    assert s.premium_accounts == [10]
    assert s.account_volumes == [[10, 100]]
