from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.commands import CommandParser, CommandService
from app.config import ContentType, ScheduleRule
from app.database import Database
from app.publisher import PublishOutcome
from app.queueing import RefillResult
from app.store import Store


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class FakeReader:
    def __init__(self, store):
        self.store = store
        self.windows = []
        self.added = []

    async def add_source(self, link):
        self.added.append(link)
        return await self.store.add_source(
            link,
            title="Anime",
            telegram_chat_id=-1000000000001,
            join_state="joined",
        )

    async def scan_source(self, source, window):
        self.windows.append(window)
        return SimpleNamespace(scanned_messages=20, inserted_candidates=7, duplicates=13)


class FakePlanner:
    def __init__(self):
        self.refills = []
        self.refreshes = []

    async def refill(self, now, *, target=None, top_mode=False):
        self.refills.append((now, target, top_mode))
        return RefillResult(added=5, unfilled=0)

    async def refresh(self, now, *, target=None, top_mode=False):
        self.refreshes.append((now, target, top_mode))
        return RefillResult(added=5, unfilled=0)


class FakePublisher:
    def __init__(self):
        self.links = []

    async def publish_now(self, link):
        self.links.append(link)
        return PublishOutcome("published", candidate_id=7, max_message_ids=("max-1",))


def update(text, *, from_id=8235497168, username="pythonvoin"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "chat": {"id": from_id},
            "from": {"id": from_id, "username": username},
            "text": text,
        },
    }


@pytest.fixture
async def command_setup():
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
            ScheduleRule(datetime.strptime("08:00", "%H:%M").time(), ContentType.ANY),
            ScheduleRule(datetime.strptime("14:00", "%H:%M").time(), ContentType.VIDEO),
        ]
    )
    bot = FakeBot()
    reader = FakeReader(store)
    planner = FakePlanner()
    publisher = FakePublisher()
    sync_calls = []

    async def sync_schedule():
        sync_calls.append(True)

    service = CommandService(
        owner_id=8235497168,
        store=store,
        reader=reader,
        planner=planner,
        publisher=publisher,
        bot=bot,
        schedule_sync=sync_schedule,
    )
    yield service, store, bot, reader, planner, publisher, sync_calls
    await database.close()


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("/status", "status"),
        ("/add_source https://t.me/source", "add_source"),
        ("https://t.me/source", "add_source"),
        ("/sources", "sources"),
        ("/del_source 1", "del_source"),
        ("/parse", "parse"),
        ("/parse 100", "parse"),
        ("/parse_period 7d 50", "parse_period"),
        ("/parse_from 14.08.2026", "parse_from"),
        ("/parse_top 30d 20", "parse_top"),
        ("/build", "parse"),
        ("/queue 10", "queue"),
        ("/refresh", "refresh"),
        ("/now https://t.me/source/7", "now"),
        ("/times 08:00,14:00", "times"),
        ("/slot 14:00 video", "slot"),
        ("/signature", "signature"),
        ("/signature НАШ ТГК | https://t.me/webm4ik", "signature"),
        ("/signature_reset", "signature_reset"),
    ],
)
def test_parser_recognizes_owner_commands_and_bare_source_link(text, name):
    assert CommandParser.parse(text).name == name


async def test_username_cannot_bypass_numeric_owner_check(command_setup):
    service, _, bot, *_ = command_setup

    result = await service.handle_update(update("/status", from_id=1, username="pythonvoin"))

    assert result.ignored == "unauthorized"
    assert bot.messages == []


async def test_bare_link_joins_scans_and_refills_queue(command_setup):
    service, store, bot, reader, planner, *_ = command_setup

    result = await service.handle_update(update("https://t.me/animeworldmem"))

    assert result.accepted is True
    assert reader.added == ["https://t.me/animeworldmem"]
    assert len(reader.windows) == 1
    assert len(planner.refills) == 1
    assert (await store.list_sources())[0].join_state == "joined"
    assert "Добавляю источник" in bot.messages[0][1]
    assert "Источник добавлен" in bot.messages[-1][1]


@pytest.mark.parametrize(
    ("command", "expected_limit", "since_days", "top_mode"),
    [
        ("/parse 100", 100, None, False),
        ("/parse_period 7d 50", 50, 7, False),
        ("/parse_top 30d 20", 20, 30, True),
    ],
)
async def test_parse_commands_scan_all_sources_and_fill_queue(
    command_setup, command, expected_limit, since_days, top_mode
):
    service, store, bot, reader, planner, *_ = command_setup
    await store.add_source("https://t.me/source", telegram_chat_id=-1001, join_state="joined")

    await service.handle_update(update(command))

    assert reader.windows[-1].limit == expected_limit
    if since_days is None:
        assert reader.windows[-1].since is None
    else:
        age = datetime.now(UTC) - reader.windows[-1].since
        assert since_days - 0.1 < age.total_seconds() / 86400 < since_days + 0.1
    assert planner.refills[-1][2] is top_mode
    assert "Добавлено кандидатов: 7" in bot.messages[-1][1]


async def test_parse_from_uses_moscow_calendar_midnight(command_setup):
    service, store, _, reader, *_ = command_setup
    await store.add_source("https://t.me/source", telegram_chat_id=-1001, join_state="joined")

    await service.handle_update(update("/parse_from 14.08.2026 33"))

    assert reader.windows[-1].limit == 33
    assert reader.windows[-1].since == datetime(2026, 8, 13, 21, tzinfo=UTC)
    assert reader.windows[-1].use_cursor is False


async def test_times_preserve_existing_rule_and_new_slots_default_to_any(command_setup):
    service, store, _, _, planner, _, sync_calls = command_setup

    await service.handle_update(update("/times 10:00,14:00,21:00"))
    rows = await store.list_schedule()

    assert [(row.local_time, row.content_type) for row in rows] == [
        ("10:00", "any"),
        ("14:00", "video"),
        ("21:00", "any"),
    ]
    assert len(sync_calls) == 1
    assert len(planner.refreshes) == 1


async def test_slot_changes_content_rule_and_rebuilds_queue(command_setup):
    service, store, _, _, planner, _, sync_calls = command_setup

    await service.handle_update(update("/slot 14:00 image"))

    rows = await store.list_schedule()
    assert [(row.local_time, row.content_type) for row in rows] == [
        ("08:00", "any"),
        ("14:00", "image"),
    ]
    assert len(sync_calls) == 1
    assert len(planner.refreshes) == 1


async def test_signature_show_change_reset_and_validation(command_setup):
    service, store, bot, *_ = command_setup

    await service.handle_update(update("/signature НОВЫЙ ТГ | https://example.com/new"))
    changed = await store.get_settings()
    assert (changed.signature_text, changed.signature_url) == (
        "НОВЫЙ ТГ",
        "https://example.com/new",
    )

    await service.handle_update(update("/signature"))
    assert "НОВЫЙ ТГ" in bot.messages[-1][1]

    await service.handle_update(update("/signature_reset"))
    reset = await store.get_settings()
    assert (reset.signature_text, reset.signature_url) == (
        "НАШ ТГК",
        "https://t.me/webm4ik",
    )

    await service.handle_update(update("/signature ПЛОХАЯ | javascript:alert(1)"))
    assert "Ошибка" in bot.messages[-1][1]


async def test_now_reports_immediate_result_and_does_not_call_planner(command_setup):
    service, _, bot, _, planner, publisher, _ = command_setup

    await service.handle_update(update("/now https://t.me/source/7"))

    assert publisher.links == ["https://t.me/source/7"]
    assert planner.refreshes == []
    assert "Опубликовано сразу" in bot.messages[-1][1]


async def test_invalid_command_returns_actionable_error(command_setup):
    service, _, bot, *_ = command_setup
    await service.handle_update(update("/slot 99:99 audio"))
    assert "Ошибка" in bot.messages[-1][1]


async def test_conflicting_long_command_reports_existing_job(command_setup):
    service, store, bot, reader, *_ = command_setup
    token = await store.acquire_job_lock(
        "content-maintenance", datetime.now(UTC), lease_seconds=600
    )

    await service.handle_update(update("/parse"))

    assert token
    assert reader.windows == []
    assert "уже выполняется" in bot.messages[-1][1]
