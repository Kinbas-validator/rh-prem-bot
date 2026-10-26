from unittest.mock import patch

from rh_prem_bot.config import Settings
from rh_prem_bot.setup_options import installation_options, update_env_template


async def test_installation_defaults_require_only_enter():
    with patch("builtins.input", return_value=""):
        cfg = await installation_options(Settings())
    assert cfg == Settings()


async def test_installation_accepts_custom_rwa_thresholds_and_retries_invalid_input():
    answers = iter(
        ["bad", "2", "openai, nvda", "nan", "-1", "12000.5", "3.5", "2.2", "4", "0", "180"]
    )
    with patch("builtins.input", side_effect=lambda prompt: next(answers)):
        cfg = await installation_options(Settings())
    assert cfg.rwa_categories == () and cfg.markets == ("OPENAI", "NVDA")
    assert cfg.min_premium_usd == 12000.5 and cfg.spike_multiplier == 3.5
    assert cfg.min_premium_accounts == 4 and cfg.cooldown_seconds == 180


def test_env_template_updates_exact_keys_and_keeps_comments():
    template = "# TOKEN=comment\nTOKEN=old\nOTHER=keep\n"
    assert update_env_template(template, {"TOKEN": "new", "ADDED": "1"}) == (
        "# TOKEN=comment\nTOKEN=new\nOTHER=keep\nADDED=1\n"
    )
