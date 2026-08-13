from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import Database
from app.store import Store
from app.telegram_reader import (
    ScanWindow,
    TelegramFloodWait,
    TelegramJoinPending,
    TelegramReader,
)


class UserAlreadyParticipantError(Exception):
    pass


class InviteRequestSentError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class FakeClient:
    def __init__(self):
        self.entity = SimpleNamespace(id=777, title="Anime source")
        self.messages = []
        self.requests = []
        self.join_error = None
        self.iter_error = None
        self.authorized = True
        self.downloaded = []

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=42)

    async def get_entity(self, value):
        self.resolved = value
        return self.entity

    async def __call__(self, request):
        self.requests.append(type(request).__name__)
        if self.join_error and type(request).__name__ != "CheckChatInviteRequest":
            raise self.join_error
        if type(request).__name__ == "CheckChatInviteRequest":
            return SimpleNamespace(title="Private anime")
        return SimpleNamespace(chats=[self.entity])

    async def iter_messages(self, entity, **kwargs):
        self.iter_entity = entity
        self.iter_kwargs = kwargs
        if self.iter_error:
            raise self.iter_error
        for message in self.messages[: kwargs.get("limit")]:
            yield message

    async def get_messages(self, entity, ids):
        wanted = {ids} if isinstance(ids, int) else set(ids)
        found = [message for message in self.messages if message.id in wanted]
        return found[0] if isinstance(ids, int) and found else found

    async def download_media(self, message, file):
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(message.download_bytes)
        self.downloaded.append((message.id, path))
        return str(path)


def text_entity(kind, offset, length, url=None):
    return SimpleNamespace(type=kind, offset=offset, length=length, url=url)


def video_message(message_id, *, grouped_id=None, date=None, text="Пост"):
    document = SimpleNamespace(id=10_000 + message_id, size=25 * 1024 * 1024)
    file = SimpleNamespace(
        name=f"edit-{message_id}.mp4",
        size=25 * 1024 * 1024,
        duration=20,
        width=1920,
        height=1080,
        mime_type="video/mp4",
    )
    return SimpleNamespace(
        id=message_id,
        grouped_id=grouped_id,
        date=date or datetime(2026, 8, 14, 9, tzinfo=UTC),
        message=text,
        raw_text=text,
        entities=[],
        buttons=None,
        photo=None,
        video=document,
        gif=None,
        audio=None,
        voice=None,
        document=document,
        file=file,
        views=100 + message_id,
        forwards=5,
        reactions=SimpleNamespace(results=[SimpleNamespace(count=7), SimpleNamespace(count=3)]),
        download_bytes=(b"video-data-" + str(message_id).encode()) * 100,
    )


@pytest.fixture
async def reader_setup():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.start()
    store = Store(database)
    client = FakeClient()
    reader = TelegramReader(
        api_id=123,
        api_hash="fake-hash",
        session_string="fake-session",
        store=store,
        client_factory=lambda: client,
    )
    yield reader, client, store
    await database.close()


async def test_verify_session_returns_numeric_account_id(reader_setup):
    reader, _, _ = reader_setup
    assert await reader.verify_session() == 42


async def test_add_public_source_joins_and_persists_peer(reader_setup):
    reader, client, store = reader_setup

    source = await reader.add_source("https://t.me/animeworldmem")

    assert source.telegram_chat_id == -1000000000777
    assert source.join_state == "joined"
    assert client.resolved == "animeworldmem"
    assert client.requests == ["JoinChannelRequest"]
    assert (await store.list_sources(enabled_only=True))[0].link.endswith("/animeworldmem")


async def test_already_joined_public_source_is_success(reader_setup):
    reader, client, _ = reader_setup
    client.join_error = UserAlreadyParticipantError()
    source = await reader.add_source("@animeworldmem")
    assert source.join_state == "joined"


async def test_private_invite_waiting_for_approval_is_persisted_pending(reader_setup):
    reader, client, store = reader_setup
    client.join_error = InviteRequestSentError()

    with pytest.raises(TelegramJoinPending):
        await reader.add_source("https://t.me/+privatehash")

    sources = await store.list_sources()
    assert len(sources) == 1
    assert sources[0].join_state == "pending_join"
    assert client.requests == ["CheckChatInviteRequest", "ImportChatInviteRequest"]


async def test_flood_wait_does_not_create_active_source(reader_setup):
    reader, client, store = reader_setup
    client.join_error = FloodWaitError(120)

    with pytest.raises(TelegramFloodWait, match="120"):
        await reader.add_source("https://t.me/animeworldmem")

    assert await store.list_sources(enabled_only=True) == []


async def test_scan_groups_album_and_persists_one_candidate(reader_setup):
    reader, client, store = reader_setup
    source = await store.add_source(
        "https://t.me/animeworldmem",
        telegram_chat_id=-1000000000777,
        join_state="joined",
    )
    client.messages = [
        video_message(11, grouped_id=99, text=""),
        video_message(10, grouped_id=99, text="Альбом https://bad.example"),
    ]

    result = await reader.scan_source(source, ScanWindow(limit=100))
    candidates = await store.list_unqueued_candidates()

    assert result.scanned_messages == 2
    assert result.inserted_candidates == 1
    assert result.items[0].message_ids == (10, 11)
    assert len(candidates) == 1
    assert candidates[0].cleaned_html == "Альбом"
    assert candidates[0].reactions == 20
    assert candidates[0].media_kind == "video"


async def test_scan_respects_period_and_cursor(reader_setup):
    reader, client, store = reader_setup
    source = await store.add_source(
        "https://t.me/animeworldmem",
        telegram_chat_id=-1000000000777,
        join_state="joined",
    )
    client.messages = [
        video_message(30, date=datetime(2026, 8, 14, tzinfo=UTC)),
        video_message(20, date=datetime(2026, 8, 10, tzinfo=UTC)),
    ]
    since = datetime(2026, 8, 13, tzinfo=UTC)

    result = await reader.scan_source(source, ScanWindow(limit=50, since=since))
    refreshed = await store.get_source(source.id)

    assert result.scanned_messages == 1
    assert refreshed.cursor_message_id == 30


async def test_scan_flood_wait_persists_retry_time(reader_setup):
    reader, client, store = reader_setup
    source = await store.add_source(
        "https://t.me/animeworldmem",
        telegram_chat_id=-1000000000777,
        join_state="joined",
    )
    client.iter_error = FloodWaitError(90)

    with pytest.raises(TelegramFloodWait, match="90"):
        await reader.scan_source(source, ScanWindow(limit=50))

    refreshed = await store.get_source(source.id)
    assert refreshed.retry_after > datetime.now(UTC) + timedelta(seconds=80)


async def test_download_candidate_writes_large_media_to_disk(reader_setup, tmp_path):
    reader, client, store = reader_setup
    source = await store.add_source(
        "https://t.me/animeworldmem",
        telegram_chat_id=-1000000000777,
        join_state="joined",
    )
    client.messages = [video_message(10)]
    await reader.scan_source(source, ScanWindow(limit=10))
    candidate = (await store.list_unqueued_candidates())[0]

    files = await reader.download_candidate(candidate, tmp_path)

    assert len(files) == 1
    assert files[0].stat().st_size > 20
    assert files[0].read_bytes().startswith(b"video-data-10")
    assert client.downloaded[0][0] == 10
