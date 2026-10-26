import math
from dataclasses import replace

import pytest

from rh_prem_bot.__main__ import health, instance_lock
from rh_prem_bot.config import Settings
from rh_prem_bot.lighter import now_ms
from rh_prem_bot.store import Store


@pytest.mark.parametrize(
    "changes",
    [
        {"spike_multiplier": math.nan},
        {"min_premium_usd": math.inf},
        {"min_premium_share": -0.1},
        {"warmup_minutes": 31},
        {"flow_direction": "invalid"},
        {"market_types": ("bo",)},
        {"retention_days": 0},
        {"window_seconds": 999999},
    ],
)
def test_invalid_settings(changes):
    with pytest.raises(ValueError):
        replace(Settings(), **changes).validate()


def test_monitor_needs_no_private_key_but_telegram_needs_credentials():
    Settings().validate()
    with pytest.raises(ValueError, match="setup"):
        Settings().validate(telegram=True)


def test_token_not_in_config_repr():
    assert "SECRET" not in repr(Settings(token="SECRET"))


def test_dotenv_market_filters_and_telegram_ids(tmp_path, monkeypatch):
    for name in Settings.__dataclass_fields__:
        monkeypatch.delenv(name.upper(), raising=False)
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS", "TELEGRAM_ADMIN_IDS"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "MARKETS=openai, nvda\nRWA_CATEGORIES=stock,pre_ipo\n"
        "TELEGRAM_BOT_TOKEN=TEST\nTELEGRAM_CHAT_IDS=123,-100\nMIN_PREMIUM_USD=12345\n"
    )
    cfg = Settings.from_env(env)
    assert cfg.markets == ("OPENAI", "NVDA")
    assert cfg.admin_ids == (123,)
    assert cfg.chat_ids == (123, -100) and cfg.min_premium_usd == 12345


def test_single_instance_lock_and_release(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    with instance_lock(path):
        with pytest.raises(ValueError, match="другой экземпляр"), instance_lock(path):
            pass
    with instance_lock(path):
        pass


def test_fractional_float_configuration_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIKE_MULTIPLIER", "3.5")
    monkeypatch.setenv("MIN_PREMIUM_USD", "10000.25")
    monkeypatch.setenv("BASELINE_FLOOR_USD", "999.5")
    monkeypatch.setenv("EVALUATION_SECONDS", "0.5")
    cfg = Settings.from_env(tmp_path / "nonexistent.env")
    assert cfg.spike_multiplier == 3.5 and cfg.min_premium_usd == 10000.25
    assert cfg.baseline_floor_usd == 999.5 and cfg.evaluation_seconds == 0.5


def test_health_checks_process_age_connection_and_subscriptions(tmp_path, capsys):
    cfg = Settings(db_path=str(tmp_path / "bot.sqlite3"))
    assert health(cfg) == 1
    store = Store(cfg.db_path)
    h = {"at_ms": now_ms(), "connected": True, "markets": 34, "subscribed": 34}
    store.set("health", h)
    assert health(cfg) == 0
    store.set("health", {**h, "at_ms": now_ms() - 120000})
    assert health(cfg) == 1
    store.set("health", {**h, "connected": False})
    assert health(cfg) == 1
    store.set("health", {**h, "subscribed": 33})
    assert health(cfg) == 1
    store.close()
