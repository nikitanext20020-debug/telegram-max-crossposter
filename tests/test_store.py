from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from app.config import ContentType, ScheduleRule
from app.database import Database
from app.domain import CandidateInput, MediaItem, MediaKind, metadata_fingerprint
from app.store import Store


@pytest.fixture
async def store():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.start()
    yield Store(database)
    await database.close()


async def make_source(store, suffix="one"):
    return await store.add_source(
        f"https://t.me/{suffix}",
        title=f"Source {suffix}",
        telegram_chat_id=-1000000000000 - sum(map(ord, suffix)),
        join_state="joined",
    )


def candidate_input(source_id, message_id=10, *, fingerprint=None, media_identity=None):
    media = (
        MediaItem(
            kind=MediaKind.VIDEO,
            telegram_file_id=f"document-{message_id}",
            telegram_unique_id=media_identity or f"unique-{message_id}",
            size=30 * 1024 * 1024,
            duration=15,
            width=1280,
            height=720,
            file_name=f"video-{message_id}.mp4",
        ),
    )
    return CandidateInput(
        source_id=source_id,
        telegram_chat_id=-1000000000000,
        message_ids=(message_id,),
        album_group_id=None,
        published_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
        cleaned_html=f"Пост {message_id}",
        media=media,
        views=100,
        forwards=5,
        reactions=20,
        metadata_fingerprint=fingerprint or metadata_fingerprint(f"Пост {message_id}", media),
    )


async def test_settings_and_schedule_survive_new_store_instance(store):
    await store.ensure_settings(
        owner_telegram_id=8235497168,
        timezone="Europe/Moscow",
        signature_text="НАШ ТГК",
        signature_url="https://t.me/webm4ik",
        max_chat_id="-123",
        queue_target=12,
        queue_low_water=6,
    )
    await store.replace_schedule(
        [
            ScheduleRule(time(8), ContentType.ANY),
            ScheduleRule(time(14), ContentType.VIDEO),
        ]
    )

    settings = await store.get_settings()
    schedule = await store.list_schedule()

    assert settings.signature_text == "НАШ ТГК"
    assert settings.signature_url == "https://t.me/webm4ik"
    assert settings.queue_target == 12
    assert [(row.local_time, row.content_type) for row in schedule] == [
        ("08:00", "any"),
        ("14:00", "video"),
    ]


async def test_disabling_source_preserves_candidates_and_duplicate_history(store):
    source = await make_source(store)
    inserted = await store.upsert_candidate(candidate_input(source.id))

    await store.disable_source(source.id)
    duplicate = await store.upsert_candidate(candidate_input(source.id))

    assert inserted.inserted is True
    assert duplicate.inserted is False
    assert duplicate.candidate_id == inserted.candidate_id
    assert (await store.get_source(source.id)).enabled is False


async def test_album_message_identity_is_unique_per_source(store):
    source = await make_source(store)
    first = candidate_input(source.id, 10)
    first = replace(first, message_ids=(10, 11), album_group_id="album-77")
    second = replace(first, message_ids=(10, 11, 12), cleaned_html="Изменённый альбом")

    inserted = await store.upsert_candidate(first)
    duplicate = await store.upsert_candidate(second)

    assert inserted.inserted is True
    assert duplicate.inserted is False
    assert duplicate.duplicate_reason == "source_post"


async def test_media_identity_and_metadata_fingerprint_block_cross_source_duplicates(store):
    first_source = await make_source(store, "one")
    second_source = await make_source(store, "two")
    original = await store.upsert_candidate(candidate_input(first_source.id, 1))

    same_media = await store.upsert_candidate(
        candidate_input(second_source.id, 2, media_identity="unique-1")
    )
    same_metadata = await store.upsert_candidate(
        candidate_input(
            second_source.id,
            3,
            fingerprint=metadata_fingerprint("Пост 1", candidate_input(first_source.id, 1).media),
        )
    )

    assert original.inserted is True
    assert same_media.duplicate_reason == "telegram_media"
    assert same_metadata.duplicate_reason == "metadata"


async def test_claim_due_item_is_atomic_and_requires_matching_lease(store):
    source = await make_source(store)
    candidate = await store.upsert_candidate(candidate_input(source.id))
    due = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    await store.enqueue(candidate.candidate_id, due, ContentType.ANY)

    first = await store.claim_due_item(due, lease_seconds=60)
    second = await store.claim_due_item(due, lease_seconds=60)

    assert first is not None
    assert second is None
    assert await store.complete_item(first.id, "wrong-token", ["max-1"]) is False
    assert await store.complete_item(first.id, first.lease_token, ["max-1"]) is True
    assert (await store.get_queue_item(first.id)).state == "published"


async def test_expired_processing_lease_can_be_claimed_again(store):
    source = await make_source(store)
    candidate = await store.upsert_candidate(candidate_input(source.id))
    due = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    await store.enqueue(candidate.candidate_id, due, ContentType.VIDEO)
    first = await store.claim_due_item(due, lease_seconds=10)

    reclaimed = await store.claim_due_item(due + timedelta(seconds=11), lease_seconds=10)

    assert reclaimed.id == first.id
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed.attempts == 2


async def test_scheduled_time_and_candidate_cannot_be_queued_twice(store):
    source = await make_source(store)
    first = await store.upsert_candidate(candidate_input(source.id, 1))
    second = await store.upsert_candidate(candidate_input(source.id, 2))
    due = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)

    assert await store.enqueue(first.candidate_id, due, ContentType.ANY) is True
    assert (
        await store.enqueue(first.candidate_id, due + timedelta(hours=1), ContentType.ANY) is False
    )
    assert await store.enqueue(second.candidate_id, due, ContentType.ANY) is False


async def test_final_hash_can_only_be_published_once(store):
    source = await make_source(store)
    first = await store.upsert_candidate(candidate_input(source.id, 1))
    second = await store.upsert_candidate(candidate_input(source.id, 2))

    assert await store.record_media_hash("a" * 64, first.candidate_id) is True
    assert await store.record_media_hash("a" * 64, second.candidate_id) is False
