from __future__ import annotations

import asyncio
import json
import mimetypes
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx


class MaxApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ):
        self.status = status
        self.code = code
        super().__init__(message)


class AmbiguousMaxSendError(RuntimeError):
    pass


class MediaLimitError(ValueError):
    pass


MEDIA_LIMITS = {
    "image": 50 * 1024 * 1024,
    "video": 250 * 1024 * 1024,
    "audio": 256 * 1024 * 1024,
    "file": 4 * 1024 * 1024 * 1024,
}


def validate_media_metadata(media: dict) -> None:
    media_type = str(media.get("kind", ""))
    if media_type not in MEDIA_LIMITS:
        raise MediaLimitError(f"MAX не поддерживает тип вложения {media_type}")
    size = media.get("size")
    if size is not None and int(size) > MEDIA_LIMITS[media_type]:
        limit_mb = MEDIA_LIMITS[media_type] // 1024 // 1024
        raise MediaLimitError(f"Вложение превышает лимит MAX {limit_mb} МБ")
    duration = media.get("duration")
    if media_type == "audio" and duration is not None and int(duration) > 3600:
        raise MediaLimitError("Аудио превышает лимит MAX 60 минут")


def _max_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    certificate = Path(__file__).resolve().parent.parent / "certs" / "russian-trusted-root-ca.pem"
    if certificate.exists():
        context.load_verify_locations(cafile=str(certificate))
    return context


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class MaxClient:
    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        api_base: str = "https://platform-api2.max.ru",
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
        timeout: float = 120,
    ):
        self.token = token
        self.chat_id = chat_id
        self.api_base = api_base.rstrip("/")
        self.sleep = sleep
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(
            transport=transport,
            verify=_max_ssl_context(),
            timeout=httpx.Timeout(timeout, connect=30),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("MAX-клиент уже закрыт")
        return self._client

    @staticmethod
    async def _response_json(response: httpx.Response) -> dict:
        await response.aread()
        if not response.content:
            return {}
        try:
            value = response.json()
            return value if isinstance(value, dict) else {"value": value}
        except json.JSONDecodeError:
            return {"message": response.text[:300]}

    async def _api_request(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        response = await self.client.request(
            method,
            f"{self.api_base}{path}",
            headers={"authorization": self.token},
            json=json_body,
        )
        data = await self._response_json(response)
        if not response.is_success or data.get("success") is False:
            raise MaxApiError(
                str(data.get("message") or data.get("error") or f"HTTP {response.status_code}"),
                status=response.status_code,
                code=data.get("code"),
            )
        return data

    def validate_media(self, path: Path, media_type: str) -> int:
        validate_media_metadata({"kind": media_type, "size": path.stat().st_size})
        size = path.stat().st_size
        return size

    async def upload(self, path: Path, media_type: str) -> dict:
        self.validate_media(path, media_type)
        slot = await self._api_request(
            "POST", f"/uploads?type={httpx.QueryParams({'value': media_type})['value']}"
        )
        upload_url = slot.get("url")
        if not upload_url:
            raise MaxApiError("MAX не вернул URL для загрузки")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            response = await self.client.post(
                upload_url,
                files={"data": (path.name, stream, content_type)},
            )
        data = await self._response_json(response)
        if not response.is_success:
            raise MaxApiError(
                str(data.get("message") or f"MAX upload HTTP {response.status_code}"),
                status=response.status_code,
                code=data.get("code"),
            )
        if media_type == "image" and data.get("photos"):
            return {"type": "image", "payload": {"photos": data["photos"]}}
        token = (
            data.get("token")
            or (data.get("payload") or {}).get("token")
            or (data.get("retval") or {}).get("token")
            or slot.get("token")
        )
        if not token:
            raise MaxApiError("MAX не вернул токен загруженного файла")
        return {"type": media_type, "payload": {"token": token}}

    async def get(self, path: str) -> dict:
        return await self._api_request("GET", path)

    async def send(self, html_text: str, attachments: list[dict]) -> str:
        path = "/messages?" + str(
            httpx.QueryParams(
                {
                    "chat_id": self.chat_id,
                    "disable_link_preview": "true",
                }
            )
        )
        body = {
            "text": html_text or None,
            "attachments": attachments or None,
            "notify": True,
            "format": "html",
        }
        delays = (0, 1, 2, 4, 8)
        for index, delay in enumerate(delays):
            if delay:
                await self.sleep(delay)
            try:
                data = await self._api_request("POST", path, json_body=body)
            except MaxApiError as error:
                if error.code == "attachment.not.ready" and index < len(delays) - 1:
                    continue
                if error.status is not None and error.status >= 500:
                    raise AmbiguousMaxSendError(
                        "MAX мог принять сообщение, но вернул серверную ошибку"
                    ) from error
                raise
            except httpx.HTTPError as error:
                raise AmbiguousMaxSendError("Ответ MAX потерян после отправки сообщения") from error
            message = data.get("message") or {}
            message_id = message.get("id") or data.get("message_id")
            if not message_id:
                raise AmbiguousMaxSendError("MAX ответил без подтверждённого ID сообщения")
            return str(message_id)
        raise MaxApiError("MAX не подготовил вложение после повторных проверок")
