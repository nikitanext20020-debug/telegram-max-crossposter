from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

from app.domain import CandidateInput, MediaItem, MediaKind, metadata_fingerprint
from app.models import Candidate, Source
from app.store import Store
from app.text import TextEntity, clean_telegram_text, render_max_html


class TelegramJoinPending(RuntimeError):
    pass


class TelegramFloodWait(RuntimeError):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"Telegram требует подождать {seconds} секунд")


@dataclass(frozen=True, slots=True)
class ScanWindow:
    limit: int = 100
    since: datetime | None = None
    until: datetime | None = None
    use_cursor: bool = True


@dataclass(frozen=True, slots=True)
class ScanResult:
    scanned_messages: int
    inserted_candidates: int
    duplicates: int
    items: tuple[CandidateInput, ...]


PUBLIC_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,}$")
PRIVATE_INVITE = re.compile(r"^[A-Za-z0-9_-]+$")
ENTITY_TYPE_NAMES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityUrl": "url",
    "MessageEntityMention": "mention",
    "MessageEntityMentionName": "text_mention",
    "MessageEntityEmail": "email",
}


def _join_error_kind(error: Exception) -> str:
    return type(error).__name__.removesuffix("Error")


def _channel_peer_id(entity) -> int:
    value = int(entity.id)
    return -(10**12 + abs(value))


def _normalize_source_link(link: str) -> tuple[str, str, bool]:
    value = link.strip()
    if value.startswith("@"):
        username = value[1:]
        if not PUBLIC_USERNAME.fullmatch(username):
            raise ValueError("Некорректное имя Telegram-канала")
        return f"https://t.me/{username}", username, False
    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or parts.netloc.casefold() not in {
        "t.me",
        "telegram.me",
    }:
        raise ValueError("Нужна ссылка t.me на Telegram-канал")
    path = parts.path.strip("/")
    if path.startswith("+"):
        invite = path[1:]
        if not PRIVATE_INVITE.fullmatch(invite):
            raise ValueError("Некорректная приватная ссылка Telegram")
        return f"https://t.me/+{invite}", invite, True
    if path.startswith("joinchat/"):
        invite = path.split("/", 1)[1]
        if not PRIVATE_INVITE.fullmatch(invite):
            raise ValueError("Некорректная приватная ссылка Telegram")
        return f"https://t.me/+{invite}", invite, True
    username = path.split("/", 1)[0]
    if not PUBLIC_USERNAME.fullmatch(username):
        raise ValueError("Некорректная ссылка Telegram-канала")
    return f"https://t.me/{username}", username, False


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _text_entities(message) -> list[TextEntity]:
    result = []
    for entity in getattr(message, "entities", None) or []:
        entity_type = getattr(entity, "type", None) or ENTITY_TYPE_NAMES.get(
            type(entity).__name__, ""
        )
        if not entity_type:
            continue
        result.append(
            TextEntity(
                entity_type,
                int(entity.offset),
                int(entity.length),
                getattr(entity, "url", None),
            )
        )
    return result


def _button_urls(messages) -> list[str]:
    result = []
    for message in messages:
        for row in getattr(message, "buttons", None) or []:
            buttons = row if isinstance(row, list | tuple) else [row]
            result.extend(button.url for button in buttons if getattr(button, "url", None))
    return result


def _media_item(message) -> MediaItem | None:
    file = getattr(message, "file", None) or SimpleNamespace()
    document = getattr(message, "document", None)
    photo = getattr(message, "photo", None)
    if getattr(message, "video", None) is not None or getattr(message, "gif", None) is not None:
        kind = MediaKind.VIDEO
        raw = getattr(message, "video", None) or getattr(message, "gif", None)
        fallback = f"video-{message.id}.mp4"
    elif photo is not None:
        kind = MediaKind.IMAGE
        raw = photo
        fallback = f"photo-{message.id}.jpg"
    elif getattr(message, "audio", None) is not None or getattr(message, "voice", None) is not None:
        kind = MediaKind.AUDIO
        raw = getattr(message, "audio", None) or getattr(message, "voice", None)
        fallback = f"audio-{message.id}.ogg"
    elif document is not None:
        kind = MediaKind.FILE
        raw = document
        fallback = f"file-{message.id}"
    else:
        return None
    identity = f"{kind.value}:{getattr(raw, 'id', '')}"
    size = getattr(file, "size", None) or getattr(raw, "size", None)
    return MediaItem(
        kind=kind,
        telegram_file_id=identity,
        telegram_unique_id=identity,
        size=int(size) if size is not None else None,
        duration=getattr(file, "duration", None),
        width=getattr(file, "width", None),
        height=getattr(file, "height", None),
        file_name=getattr(file, "name", None) or fallback,
    )


def _reaction_count(message) -> int:
    reactions = getattr(message, "reactions", None)
    return sum(int(getattr(item, "count", 0)) for item in getattr(reactions, "results", []) or [])


class TelegramReader:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_string: str,
        store: Store,
        client_factory: Callable | None = None,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_string = session_string
        self.store = store
        self.client_factory = client_factory or self._new_client

    def _new_client(self):
        return TelegramClient(
            StringSession(self.session_string),
            self.api_id,
            self.api_hash,
            receive_updates=False,
        )

    @asynccontextmanager
    async def _client(self):
        client = self.client_factory()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram StringSession не авторизован")
            yield client
        finally:
            await client.disconnect()

    async def verify_session(self) -> int:
        async with self._client() as client:
            account = await client.get_me()
            if account is None or not getattr(account, "id", None):
                raise RuntimeError("Telegram не вернул авторизованный аккаунт")
            return int(account.id)

    async def add_source(self, raw_link: str) -> Source:
        link, identifier, private = _normalize_source_link(raw_link)
        async with self._client() as client:
            if private:
                preview = await client(CheckChatInviteRequest(identifier))
                title = getattr(preview, "title", "Приватный канал")
                try:
                    joined = await client(ImportChatInviteRequest(identifier))
                except Exception as error:
                    kind = _join_error_kind(error)
                    if kind == "InviteRequestSent":
                        await self.store.add_source(link, title=title, join_state="pending_join")
                        raise TelegramJoinPending(
                            "Telegram ожидает подтверждение администратора"
                        ) from error
                    if kind == "FloodWait":
                        raise TelegramFloodWait(int(error.seconds)) from error
                    raise
                entity = joined.chats[0]
            else:
                entity = await client.get_entity(identifier)
                title = getattr(entity, "title", identifier)
                try:
                    await client(JoinChannelRequest(entity))
                except Exception as error:
                    kind = _join_error_kind(error)
                    if kind == "UserAlreadyParticipant":
                        pass
                    elif kind == "InviteRequestSent":
                        await self.store.add_source(link, title=title, join_state="pending_join")
                        raise TelegramJoinPending(
                            "Telegram ожидает подтверждение администратора"
                        ) from error
                    elif kind == "FloodWait":
                        raise TelegramFloodWait(int(error.seconds)) from error
                    else:
                        raise
            return await self.store.add_source(
                link,
                title=title,
                telegram_chat_id=_channel_peer_id(entity),
                join_state="joined",
            )

    async def scan_source(self, source: Source, window: ScanWindow) -> ScanResult:
        if not source.telegram_chat_id:
            raise ValueError("Источник ещё не присоединён")
        messages = []
        newest_seen = source.cursor_message_id or 0
        try:
            async with self._client() as client:
                kwargs = {"limit": window.limit}
                if window.use_cursor and source.cursor_message_id:
                    kwargs["min_id"] = source.cursor_message_id
                async for message in client.iter_messages(source.telegram_chat_id, **kwargs):
                    published_at = _aware_utc(message.date)
                    if window.until and published_at > _aware_utc(window.until):
                        continue
                    if window.since and published_at < _aware_utc(window.since):
                        continue
                    messages.append(message)
                    newest_seen = max(newest_seen, int(message.id))
        except Exception as error:
            if _join_error_kind(error) == "FloodWait":
                seconds = int(error.seconds)
                await self.store.update_source_scan(
                    source.id,
                    cursor_message_id=source.cursor_message_id,
                    last_scanned_at=datetime.now(UTC),
                    retry_after=datetime.now(UTC) + timedelta(seconds=seconds),
                )
                raise TelegramFloodWait(seconds) from error
            raise

        grouped: dict[str, list] = defaultdict(list)
        for message in messages:
            grouped_id = getattr(message, "grouped_id", None)
            key = f"album:{grouped_id}" if grouped_id else f"message:{message.id}"
            grouped[key].append(message)

        inserted = 0
        duplicates = 0
        items = []
        for group in grouped.values():
            ordered = sorted(group, key=lambda item: item.id)
            caption_message = next(
                (
                    message
                    for message in ordered
                    if (getattr(message, "message", None) or getattr(message, "raw_text", None))
                ),
                ordered[0],
            )
            text = getattr(caption_message, "message", None) or getattr(
                caption_message, "raw_text", ""
            )
            clean = clean_telegram_text(
                text,
                _text_entities(caption_message),
                button_urls=_button_urls(ordered),
            )
            cleaned_html = render_max_html(clean)
            media = tuple(item for item in (_media_item(message) for message in ordered) if item)
            candidate = CandidateInput(
                source_id=source.id,
                telegram_chat_id=source.telegram_chat_id,
                message_ids=tuple(message.id for message in ordered),
                album_group_id=str(ordered[0].grouped_id)
                if getattr(ordered[0], "grouped_id", None)
                else None,
                published_at=min(_aware_utc(message.date) for message in ordered),
                cleaned_html=cleaned_html,
                media=media,
                views=sum(int(getattr(message, "views", 0) or 0) for message in ordered),
                forwards=sum(int(getattr(message, "forwards", 0) or 0) for message in ordered),
                reactions=sum(_reaction_count(message) for message in ordered),
                metadata_fingerprint=metadata_fingerprint(cleaned_html, media),
            )
            outcome = await self.store.upsert_candidate(candidate)
            if outcome.inserted:
                inserted += 1
                items.append(candidate)
            else:
                duplicates += 1

        await self.store.update_source_scan(
            source.id,
            cursor_message_id=newest_seen or source.cursor_message_id,
            last_scanned_at=datetime.now(UTC),
        )
        return ScanResult(len(messages), inserted, duplicates, tuple(items))

    async def get_post(self, link: str) -> tuple[Source, object]:
        normalized, username, private = _normalize_source_link(link.rsplit("/", 1)[0])
        if private:
            raise ValueError("Ссылка на пост должна содержать публичное имя канала")
        try:
            message_id = int(link.rstrip("/").rsplit("/", 1)[1])
        except (IndexError, ValueError) as error:
            raise ValueError("Некорректная ссылка на Telegram-пост") from error
        async with self._client() as client:
            entity = await client.get_entity(username)
            message = await client.get_messages(entity, ids=message_id)
        if not message:
            raise ValueError("Telegram-пост не найден")
        source = await self.store.add_source(
            normalized,
            title=getattr(entity, "title", username),
            telegram_chat_id=_channel_peer_id(entity),
            join_state="joined",
        )
        return source, message

    async def download_candidate(self, candidate: Candidate, directory: Path) -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        async with self._client() as client:
            messages = await client.get_messages(
                candidate.telegram_chat_id, ids=list(candidate.message_ids)
            )
            if not isinstance(messages, list | tuple):
                messages = [messages]
            by_id = {message.id: message for message in messages if message}
            paths = []
            for index, media in enumerate(candidate.media_json):
                message_id = candidate.message_ids[min(index, len(candidate.message_ids) - 1)]
                message = by_id.get(message_id)
                if message is None:
                    raise ValueError(f"Telegram-медиа {message_id} больше недоступно")
                safe_name = re.sub(
                    r"[^\w. -]", "_", media.get("file_name") or f"media-{message_id}"
                )[:180]
                target = directory / f"{index:02d}-{safe_name}"
                downloaded = await client.download_media(message, file=str(target))
                if not downloaded:
                    raise RuntimeError(f"Не удалось скачать Telegram-медиа {message_id}")
                paths.append(Path(downloaded))
            return paths
