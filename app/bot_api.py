from __future__ import annotations

import httpx


class BotApiError(RuntimeError):
    pass


def _chunks(text: str, limit: int = 4000) -> list[str]:
    if not text:
        return [""]
    result = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            natural = text.rfind("\n", start, end)
            if natural > start + limit // 2:
                end = natural + 1
        result.append(text[start:end])
        start = end
    return result


class BotApi:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(transport=transport, timeout=30)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict) -> object:
        try:
            response = await self.client.post(f"{self.base_url}/{method}", json=payload)
        except httpx.HTTPError as error:
            raise BotApiError("Telegram Bot API недоступен") from error
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.is_success or not data.get("ok"):
            raise BotApiError(
                str(data.get("description") or f"Telegram HTTP {response.status_code}")
            )
        return data.get("result")

    async def send_message(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text):
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )
