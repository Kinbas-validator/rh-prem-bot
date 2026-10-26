import pytest

from rh_prem_bot.models import PREMIUM_MAKER, PREMIUM_TAKER, Tier, Trade, classify_fee


@pytest.mark.parametrize("fee", sorted(PREMIUM_MAKER))
def test_all_rh_premium_maker_tiers(fee):
    assert classify_fee(fee, maker=True) == Tier.PREMIUM


@pytest.mark.parametrize("fee", sorted(PREMIUM_TAKER))
def test_all_rh_premium_taker_tiers(fee):
    assert classify_fee(fee, maker=False) == Tier.PREMIUM


@pytest.mark.parametrize(
    "maker,fee,tier",
    [
        (True, 0, Tier.STANDARD),
        (False, 0, Tier.STANDARD),
        (False, 100, Tier.STANDARD),
        (True, 100, Tier.UNKNOWN),
        (True, 150, Tier.PLUS),
        (False, 150, Tier.PLUS),
        (True, 28, Tier.UNKNOWN),
        (False, 196, Tier.UNKNOWN),
        (True, -1, Tier.UNKNOWN),
        (False, 10000, Tier.UNKNOWN),
        (True, None, Tier.UNKNOWN),
        (False, True, Tier.UNKNOWN),
        (True, "120", Tier.UNKNOWN),
        (True, 120.0, Tier.UNKNOWN),
    ],
)
def test_no_fee_shortcut_or_core_schedule(maker, fee, tier):
    assert classify_fee(fee, maker=maker) == tier


def test_roles_swap_with_maker_bid(fill):
    t = fill(is_maker_ask=False, maker_fee=0)
    assert t.maker_account == 202 and t.taker_account == 101
    assert t.flow("maker_taker") == (Tier.STANDARD, Tier.PREMIUM)
    assert t.flow("seller_buyer") == (Tier.PREMIUM, Tier.STANDARD)


def test_usd_amount_and_large_string_id_used(fill):
    t = fill(trade_id_str="9999999999999999999", price="7", size="9")
    assert t.id == "9999999999999999999"
    assert t.usd_micros == 100_000_001


def test_omitted_zero_fields():
    t = Trade.parse(
        {
            "trade_id": 2,
            "market_id": 42,
            "timestamp": 100,
            "usd_amount": "10",
            "ask_account_id": 0,
            "bid_account_id": 1,
            "is_maker_ask": True,
        }
    )
    assert t.maker_tier == t.taker_tier == Tier.STANDARD


@pytest.mark.parametrize(
    "patch",
    [
        {"usd_amount": "NaN"},
        {"usd_amount": "Infinity"},
        {"usd_amount": "-1"},
        {"usd_amount": "0"},
        {"usd_amount": "bad"},
        {"is_maker_ask": "false"},
        {"ask_account_id": -1},
        {"trade_id": True},
        {"market_id": 1.2},
        {"type": "liquidation"},
        {"type": "deleverage"},
        {"timestamp": -1},
    ],
)
def test_reject_invalid_or_non_regular_fills(fill, patch):
    with pytest.raises((ValueError, TypeError)):
        fill(**patch)
