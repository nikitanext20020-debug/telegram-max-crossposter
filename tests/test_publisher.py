import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import ContentType
from app.database import Database
from app.domain import CandidateInput, MediaItem, MediaKind, metadata_fingerprint
from app.max_client import AmbiguousMaxSendError
from app.publisher import Publisher
from app.store import Store


class FakeReader:
    def __init__(self, content=b"video"):
        self.content = content
        self.directories = []

    async def download_candidate(self, candidate, directory):
        self.directories.append(Path(directory))
        path = Path(directory) / "edit.mp4"
        path.write_bytes(self.content)
        return [path]


class SparseLargeReader(FakeReader):
    async def download_candidate(self, candidate, directory):
        self.directories.append(Path(directory))
        path = Path(directory) / "large-edit.mp4"
        with path.open("wb") as stream:
            stream.seek(21 * 1024 * 1024)
            stream.write(b"x")
        return [path]


class FakeMaxClient:
    def __init__(self, *, send_error=None, upload_error=None):
        self.send_error = send_error
        self.upload_error = upload_error
        self.uploaded = []
        self.sent = []

    def validate_media(self, path, media_type):
        return Path(path).stat().st_size

    async def upload(self, path, media_type):
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append((Path(path).stat().st_size, media_type))
        return {"type": media_type, "payload": {"token": "uploaded"}}

    async def send(self, html, attachments):
        self.sent.append((html, attachments))
        if self.send_error:
            raise self.send_error
        return f"max-{len(self.sent)}"


@pytest.fixture
async def publish_store():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.start()
    store = Store(database)
    await store.ensure_settings(
        owner_telegram_id=8235497168,
        timezone="Europe/Moscow",
        signature_text="НАШ ТГК",
        signature_url="https://t.me/webm4ik",
        max_chat_id="-1",
        queue_target=12,
        queue_low_water=6,
    )
    source = await store.add_source(
        "https://t.me/source", telegram_chat_id=-1001, join_state="joined"
    )
    yield store, source
    await database.close()


async def queued_video(store, source, message_id=1, due=None):
    media = (
        MediaItem(
            kind=MediaKind.VIDEO,
            telegram_file_id=f"video:{message_id}",
            telegram_unique_id=f"video:{message_id}",
            size=21 * 1024 * 1024,
            duration=20,
            width=1920,
            height=1080,
            file_name="edit.mp4",
        ),
    )
    inserted = await store.upsert_candidate(
        CandidateInput(
            source_id=source.id,
            telegram_chat_id=source.telegram_chat_id,
            message_ids=(message_id,),
            album_group_id=None,
            published_at=datetime(2026, 8, 14, tzinfo=UTC),
            cleaned_html="Пост",
            media=media,
            metadata_fingerprint=metadata_fingerprint(f"Пост {message_id}", media),
        )
    )
    due = due or datetime(2026, 8, 14, 8, tzinfo=UTC)
    await store.enqueue(inserted.candidate_id, due, ContentType.VIDEO)
    return inserted.candidate_id, due


async def test_publishes_file_larger_than_20mb_and_completes_queue(publish_store):
    store, source = publish_store
    _, due = await queued_video(store, source)
    reader = SparseLargeReader()
    max_client = FakeMaxClient()
    publisher = Publisher(store, reader, max_client)

    outcome = await publisher.publish_due(due)
    rows = await store.list_queue()

    assert outcome.state == "published"
    assert max_client.uploaded == [(21 * 1024 * 1024 + 1, "video")]
    assert max_client.sent[0][0] == ('Пост\n\n<a href="https://t.me/webm4ik">НАШ ТГК</a>')
    assert rows[0].state == "published"
    assert not reader.directories[0].exists()


async def test_duplicate_final_hash_is_skipped_before_max_send(publish_store):
    store, source = publish_store
    candidate_id, due = await queued_video(store, source)
    content = b"same-final-file"
    previous_media = (
        MediaItem(
            kind=MediaKind.VIDEO,
            telegram_file_id="video:previous",
            telegram_unique_id="video:previous",
            size=len(content),
            duration=1,
            width=100,
            height=100,
            file_name="previous.mp4",
        ),
    )
    previous = await store.upsert_candidate(
        CandidateInput(
            source_id=source.id,
            telegram_chat_id=source.telegram_chat_id,
            message_ids=(999,),
            album_group_id=None,
            published_at=datetime(2026, 8, 13, tzinfo=UTC),
            cleaned_html="Предыдущий пост",
            media=previous_media,
            metadata_fingerprint=metadata_fingerprint("Предыдущий пост", previous_media),
        )
    )
    await store.record_media_hash(hashlib.sha256(content).hexdigest(), previous.candidate_id)
    reader = FakeReader(content)
    max_client = FakeMaxClient()

    outcome = await Publisher(store, reader, max_client).publish_due(due)

    assert outcome.state == "skipped_duplicate"
    assert max_client.sent == []
    assert (await store.list_queue())[0].state == "skipped_duplicate"


async def test_signature_change_applies_to_already_queued_post(publish_store):
    store, source = publish_store
    _, due = await queued_video(store, source)
    await store.update_signature("НОВАЯ ПОДПИСЬ", "https://example.com/new")
    max_client = FakeMaxClient()

    await Publisher(store, FakeReader(), max_client).publish_due(due)

    assert max_client.sent[0][0].endswith('<a href="https://example.com/new">НОВАЯ ПОДПИСЬ</a>')


async def test_temporary_files_are_removed_when_upload_fails(publish_store):
    store, source = publish_store
    _, due = await queued_video(store, source)
    reader = FakeReader()
    max_client = FakeMaxClient(upload_error=RuntimeError("upload failed"))

    outcome = await Publisher(store, reader, max_client).publish_due(due)

    assert outcome.state == "failed"
    assert not reader.directories[0].exists()


async def test_ambiguous_max_send_is_never_automatically_retried(publish_store):
    store, source = publish_store
    _, due = await queued_video(store, source)
    error = AmbiguousMaxSendError("response lost")
    max_client = FakeMaxClient(send_error=error)

    outcome = await Publisher(store, FakeReader(), max_client).publish_due(due)

    assert outcome.state == "ambiguous"
    assert len(max_client.sent) == 1
    assert (await store.list_queue())[0].state == "ambiguous"
