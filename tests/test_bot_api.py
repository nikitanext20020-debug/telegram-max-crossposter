import httpx
import pytest

from app.bot_api import BotApi, BotApiError


async def test_sends_long_messages_in_safe_chunks():
    payloads = []

    async def handler(request):
        payloads.append(__import__("json").loads((await request.aread()).decode()))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = BotApi("token", transport=httpx.MockTransport(handler))
    try:
        await bot.send_message(8235497168, "А" * 5000)
    finally:
        await bot.close()

    assert len(payloads) == 2
    assert all(payload["chat_id"] == 8235497168 for payload in payloads)
    assert "".join(payload["text"] for payload in payloads) == "А" * 5000


async def test_raises_without_echoing_token_on_telegram_error():
    async def handler(request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    bot = BotApi("secret-token", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BotApiError) as raised:
            await bot.send_message(1, "test")
    finally:
        await bot.close()

    assert "Unauthorized" in str(raised.value)
    assert "secret-token" not in str(raised.value)
