from datetime import time
from types import SimpleNamespace

import pytest

from app.config import ContentType, ScheduleRule
from app.qstash import QStashService, cron_for_slot


class FakeReceiver:
    def __init__(self):
        self.calls = []
        self.error = None

    def verify(self, *, signature, body, url):
        self.calls.append((signature, body, url))
        if self.error:
            raise self.error


class FakeScheduleApi:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.existing = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return kwargs["schedule_id"]

    def list(self):
        return self.existing

    def delete(self, schedule_id):
        self.deleted.append(schedule_id)


class FakeQStashClient:
    def __init__(self):
        self.schedule = FakeScheduleApi()


def test_publish_cron_uses_moscow_timezone():
    assert cron_for_slot(time(14, 0)) == "CRON_TZ=Europe/Moscow 0 14 * * *"


def test_verify_forwards_exact_raw_body_signature_and_public_url():
    receiver = FakeReceiver()
    service = QStashService(
        token="token",
        current_signing_key="current",
        next_signing_key="next",
        public_url="https://service.example",
        receiver=receiver,
        client=FakeQStashClient(),
    )

    service.verify(
        body=b'{"slot":"14:00"}',
        signature="signed-jwt",
        path_and_query="/jobs/publish?slot=14%3A00",
    )

    assert receiver.calls == [
        (
            "signed-jwt",
            '{"slot":"14:00"}',
            "https://service.example/jobs/publish?slot=14%3A00",
        )
    ]


def test_missing_or_invalid_signature_is_rejected():
    receiver = FakeReceiver()
    receiver.error = ValueError("bad signature")
    service = QStashService(
        token="token",
        current_signing_key="current",
        next_signing_key="next",
        public_url="https://service.example",
        receiver=receiver,
        client=FakeQStashClient(),
    )
    with pytest.raises(ValueError, match="bad signature"):
        service.verify(body=b"{}", signature="bad", path_and_query="/jobs/maintain")


async def test_reconcile_creates_six_publish_schedules_and_one_maintenance():
    client = FakeQStashClient()
    client.schedule.existing = [
        SimpleNamespace(schedule_id="tgmax-publish-0300"),
        SimpleNamespace(schedule_id="unrelated-schedule"),
    ]
    service = QStashService(
        token="token",
        current_signing_key="current",
        next_signing_key="next",
        public_url="https://service.example",
        receiver=FakeReceiver(),
        client=client,
    )
    rules = [
        ScheduleRule(time(hour), ContentType.VIDEO if hour == 14 else ContentType.ANY)
        for hour in (8, 10, 12, 14, 18, 21)
    ]

    ids = await service.reconcile(rules)

    assert ids == {
        "tgmax-maintain",
        "tgmax-publish-0800",
        "tgmax-publish-1000",
        "tgmax-publish-1200",
        "tgmax-publish-1400",
        "tgmax-publish-1800",
        "tgmax-publish-2100",
    }
    assert len(client.schedule.created) == 7
    assert client.schedule.deleted == ["tgmax-publish-0300"]
    assert all(item["method"] == "POST" for item in client.schedule.created)
    publish_14 = next(
        item for item in client.schedule.created if item["schedule_id"] == "tgmax-publish-1400"
    )
    assert publish_14["destination"].endswith("/jobs/publish?slot=14%3A00")
    assert publish_14["cron"] == "CRON_TZ=Europe/Moscow 0 14 * * *"
