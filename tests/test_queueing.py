from datetime import UTC, datetime, time, timedelta
from itertools import pairwise

import pytest

from app.config import ContentType, ScheduleRule
from app.database import Database
from app.domain import CandidateInput, MediaItem, MediaKind, metadata_fingerprint
from app.queueing import (
    CandidateChoice,
    QueuePlanner,
    choose_candidate,
    engagement_score,
    next_slots,
)
from app.store import Store


def choice(candidate_id, source_id, media_kind, engagement, hour=8):
    return CandidateChoice(
        id=candidate_id,
        source_id=source_id,
        media_kind=media_kind,
        published_at=datetime(2026, 8, 14, hour, tzinfo=UTC),
        views=engagement,
        forwards=0,
        reactions=0,
    )


def test_next_slots_are_future_moscow_times_in_utc():
    slots = next_slots(
        datetime(2026, 8, 14, 8, 30, tzinfo=UTC),
        [
            ScheduleRule(time(8), ContentType.ANY),
            ScheduleRule(time(14), ContentType.VIDEO),
            ScheduleRule(time(21), ContentType.ANY),
        ],
        4,
        "Europe/Moscow",
    )

    assert [(slot.when, slot.rule) for slot in slots] == [
        (datetime(2026, 8, 14, 11, 0, tzinfo=UTC), ContentType.VIDEO),
        (datetime(2026, 8, 14, 18, 0, tzinfo=UTC), ContentType.ANY),
        (datetime(2026, 8, 15, 5, 0, tzinfo=UTC), ContentType.ANY),
        (datetime(2026, 8, 15, 11, 0, tzinfo=UTC), ContentType.VIDEO),
    ]


def test_next_slots_uses_timezone_rules_instead_of_fixed_offset():
    winter = next_slots(
        datetime(2026, 1, 1, tzinfo=UTC),
        [ScheduleRule(time(8), ContentType.ANY)],
        1,
        "America/New_York",
    )[0]
    summer = next_slots(
        datetime(2026, 7, 1, tzinfo=UTC),
        [ScheduleRule(time(8), ContentType.ANY)],
        1,
        "America/New_York",
    )[0]
    assert winter.when.hour == 13
    assert summer.when.hour == 12


def test_video_rule_never_selects_image():
    selected = choose_candidate(
        ContentType.VIDEO,
        [choice(1, 1, "image", 100), choice(2, 2, "video", 1)],
        recent_source_ids=[],
    )
    assert selected.id == 2


def test_round_robin_source_beats_higher_engagement_from_last_source():
    selected = choose_candidate(
        ContentType.ANY,
        [choice(1, 1, "video", 10_000), choice(2, 2, "image", 1)],
        recent_source_ids=[1],
    )
    assert selected.id == 2


def test_top_mode_prioritizes_activity():
    candidates = [
        choice(1, 1, "video", 10),
        CandidateChoice(
            id=2,
            source_id=2,
            media_kind="video",
            published_at=datetime(2026, 8, 13, tzinfo=UTC),
            views=100,
            forwards=20,
            reactions=30,
        ),
    ]
    selected = choose_candidate(ContentType.ANY, candidates, recent_source_ids=[], top_mode=True)
    assert selected.id == 2
    assert engagement_score(selected, top_mode=True) > engagement_score(
        candidates[0], top_mode=True
    )


@pytest.fixture
async def planner_store():
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
    await store.replace_schedule(
        [
            ScheduleRule(time(8), ContentType.ANY),
            ScheduleRule(time(14), ContentType.VIDEO),
        ]
    )
    yield store
    await database.close()


async def add_candidates(store, source_id, start, count, kind):
    for index in range(start, start + count):
        media = (
            MediaItem(
                kind=kind,
                telegram_file_id=f"file-{source_id}-{index}",
                telegram_unique_id=f"unique-{source_id}-{index}",
                size=1000 + index,
                duration=10 if kind is MediaKind.VIDEO else None,
                width=1280,
                height=720,
                file_name=(
                    f"media-{source_id}-{index}.{'mp4' if kind is MediaKind.VIDEO else 'jpg'}"
                ),
            ),
        )
        await store.upsert_candidate(
            CandidateInput(
                source_id=source_id,
                telegram_chat_id=-1000 - source_id,
                message_ids=(index,),
                album_group_id=None,
                published_at=datetime(2026, 8, 14, tzinfo=UTC) + timedelta(minutes=index),
                cleaned_html=f"Пост {source_id}-{index}",
                media=media,
                views=index,
                metadata_fingerprint=metadata_fingerprint(f"Пост {source_id}-{index}", media),
            )
        )


async def test_refill_builds_target_and_alternates_sources(planner_store):
    first = await planner_store.add_source(
        "https://t.me/one", telegram_chat_id=-1001, join_state="joined"
    )
    second = await planner_store.add_source(
        "https://t.me/two", telegram_chat_id=-1002, join_state="joined"
    )
    await add_candidates(planner_store, first.id, 1, 8, MediaKind.VIDEO)
    await add_candidates(planner_store, second.id, 20, 8, MediaKind.VIDEO)
    planner = QueuePlanner(planner_store)

    result = await planner.refill(datetime(2026, 8, 14, tzinfo=UTC), target=12)
    rows = await planner_store.list_queue()

    assert result.added == 12
    assert len(rows) == 12
    source_ids = [row.source_id for row in rows]
    assert all(left != right for left, right in pairwise(source_ids))


async def test_refill_leaves_video_slot_empty_when_only_images_exist(planner_store):
    source = await planner_store.add_source(
        "https://t.me/images", telegram_chat_id=-1003, join_state="joined"
    )
    await add_candidates(planner_store, source.id, 1, 5, MediaKind.IMAGE)
    planner = QueuePlanner(planner_store)

    result = await planner.refill(datetime(2026, 8, 14, tzinfo=UTC), target=2)
    rows = await planner_store.list_queue()

    assert result.added == 1
    assert result.unfilled == 1
    assert rows[0].slot_rule == "any"


async def test_refresh_removes_only_unpublished_rows(planner_store):
    source = await planner_store.add_source(
        "https://t.me/videos", telegram_chat_id=-1004, join_state="joined"
    )
    await add_candidates(planner_store, source.id, 1, 8, MediaKind.VIDEO)
    planner = QueuePlanner(planner_store)
    now = datetime(2026, 8, 14, tzinfo=UTC)
    await planner.refill(now, target=4)
    due = await planner_store.list_queue()
    claimed = await planner_store.claim_due_item(due[0].scheduled_at)
    await planner_store.complete_item(claimed.id, claimed.lease_token, ["max-1"])

    await planner.refresh(now, target=4)
    after = await planner_store.list_queue()

    assert any(row.state == "published" and row.id == claimed.id for row in after)
    assert len([row for row in after if row.state == "pending"]) == 4
