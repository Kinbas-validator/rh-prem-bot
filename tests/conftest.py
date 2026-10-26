import pytest

from rh_prem_bot.models import Trade
from rh_prem_bot.store import Store


@pytest.fixture
def store():
    instance = Store(":memory:")
    yield instance
    instance.close()


@pytest.fixture
def fill():
    def make(**changes):
        raw = {
            "trade_id": 1,
            "market_id": 42,
            "timestamp": 1_000_000,
            "usd_amount": "100.000001",
            "ask_account_id": 101,
            "bid_account_id": 202,
            "is_maker_ask": True,
            "maker_fee": 120,
            "taker_fee": 350,
            "type": "trade",
        }
        raw.update(changes)
        return Trade.parse(raw)

    return make
