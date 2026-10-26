from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, code: int, retry_after: int = 0, *, not_modified: bool = False):
        self.code, self.retry_after = code, retry_after
        self.not_modified = not_modified
        super().__init__(f"Telegram API error {code}")


class Telegram:
    def __init__(self, session: aiohttp.ClientSession, token: str):
        self.session = session
        self._base = f"https://api.telegram.org/bot{token}/"

    async def call(self, method: str, **payload):
        try:
            async with self.session.post(
                self._base + method,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except ValueError:
                    raise TelegramError(response.status) from None
                if not data.get("ok"):
                    raise TelegramError(
                        int(data.get("error_code", response.status)),
                        int(data.get("parameters", {}).get("retry_after", 0)),
                        not_modified="message is not modified" in data.get("description", ""),
                    )
                return data.get("result")
        except aiohttp.ClientError:
            # aiohttp exceptions can include the token in the URL. Never expose them.
            raise TelegramError(503) from None

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", **payload)

    async def edit(self, chat_id: int, message_id: int, text: str, markup: dict):
        try:
            return await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options={"is_disabled": True},
            )
        except TelegramError as exc:
            if not exc.not_modified:
                raise

    async def check(self):
        user = await self.call("getMe")
        webhook = await self.call("getWebhookInfo")
        if webhook.get("url"):
            raise ValueError(
                "У бота активен webhook. Используйте отдельного бота для long polling."
            )
        return user

    async def poll(self, store, handler):
        offset = store.get("telegram_offset", 0)
        backoff = 1
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    await handler(update)
                    offset = update["update_id"] + 1
                    store.set("telegram_offset", offset)
                backoff = 1
            except TelegramError as exc:
                if exc.code in {401, 409}:
                    # Invalid token or another poller is a fatal deployment problem.
                    raise
                log.warning("Telegram polling unavailable (code %d)", exc.code)
                await asyncio.sleep(min(60, max(backoff, exc.retry_after)))
                backoff = min(60, backoff * 2)
            except TimeoutError:
                await asyncio.sleep(2)
