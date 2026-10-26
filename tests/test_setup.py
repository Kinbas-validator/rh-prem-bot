import asyncio
import stat
from pathlib import Path

import pytest

from rh_prem_bot.__main__ import setup
from rh_prem_bot.config import Settings


@pytest.mark.parametrize("autodiscover", [False, True])
@pytest.mark.parametrize("service_mode", [False, True])
async def test_setup_saves_private_env_without_printing_token(
    tmp_path, monkeypatch, capsys, autodiscover, service_mode
):
    template = await asyncio.to_thread(Path(".env.example").read_text)
    monkeypatch.chdir(tmp_path)
    await asyncio.to_thread(Path(".env.example").write_text, template)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "123456:FAKE_SECRET_TOKEN")
    answers = iter(["" if autodiscover else "123", "1", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    offsets, sends = [], []

    class FakeTelegram:
        def __init__(self, session, token):
            assert token == "123456:FAKE_SECRET_TOKEN"

        async def check(self):
            return {"username": "testbot"}

        async def send(self, chat_id, text):
            sends.append(chat_id)

        async def call(self, method, **payload):
            assert method == "getUpdates"
            offsets.append(payload["offset"])
            return [
                {
                    "update_id": 5 if len(offsets) == 1 else 6,
                    "message": {
                        "text": "hello" if len(offsets) == 1 else "/start",
                        "chat": {"id": 123, "type": "private", "first_name": "Test"},
                    },
                }
            ]

    monkeypatch.setattr("rh_prem_bot.__main__.Telegram", FakeTelegram)
    target = tmp_path / ".env"
    await setup(Settings(), target, advanced=service_mode, service_mode=service_mode)
    content = target.read_text()
    assert "TELEGRAM_CHAT_IDS=123" in content
    assert "TELEGRAM_ADMIN_IDS=123" in content
    assert "TELEGRAM_BOT_TOKEN=123456:FAKE_SECRET_TOKEN" in content
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    expected_db = "/var/lib/rh-prem-bot/bot.sqlite3" if service_mode else "data/bot.sqlite3"
    assert f"DB_PATH={expected_db}" in content
    assert "FAKE_SECRET_TOKEN" not in capsys.readouterr().out
    assert sends == [123]
    if autodiscover:
        assert offsets == [0, 6]
    with pytest.raises(ValueError, match="существует"):
        await setup(Settings(), target)
