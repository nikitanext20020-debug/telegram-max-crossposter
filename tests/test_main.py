from dataclasses import dataclass

import httpx
import pytest

from app.main import AppServices, RetryableJobError, create_app


class FakeDatabase:
    def __init__(self, healthy=True):
        self.is_healthy = healthy

    async def healthy(self):
        return self.is_healthy


class FakeCommands:
    def __init__(self):
        self.updates = []

    async def handle_update(self, update, *, background=None):
        self.updates.append((update, background is not None))


class FakeQStash:
    def __init__(self):
        self.calls = []
        self.error = None

    def verify(self, *, body, signature, path_and_query):
        self.calls.append((body, signature, path_and_query))
        if self.error:
            raise self.error


@dataclass
class Outcome:
    state: str


class FakeAutomation:
    def __init__(self):
        self.maintains = 0
        self.publishes = 0
        self.maintain_error = None
        self.outcomes = [Outcome("published"), Outcome("idle")]

    async def maintain(self):
        self.maintains += 1
        if self.maintain_error:
            raise self.maintain_error
        return {"scanned": 10, "queued": 5}

    async def publish(self):
        self.publishes += 1
        return self.outcomes.pop(0)


@pytest.fixture
def web_setup():
    settings = SimpleSettings()
    database = FakeDatabase()
    commands = FakeCommands()
    qstash = FakeQStash()
    automation = FakeAutomation()
    services = AppServices(
        database=database,
        commands=commands,
        qstash=qstash,
        automation=automation,
    )
    app = create_app(settings, services=services)
    return app, settings, database, commands, qstash, automation


class SimpleSettings:
    telegram_webhook_secret = "webhook_secret_123"


async def request(app, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://service.example"
    ) as client:
        return await client.request(method, path, **kwargs)


async def test_health_reports_database_reachability_without_secrets(web_setup):
    app, _, database, *_ = web_setup
    response = await request(app, "GET", "/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "database": True}
    assert "webhook_secret" not in response.text

    database.is_healthy = False
    response = await request(app, "GET", "/healthz")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "database": False}


async def test_telegram_webhook_requires_path_and_header_secret(web_setup):
    app, settings, _, commands, *_ = web_setup
    path = f"/telegram/webhook/{settings.telegram_webhook_secret}"
    update = {"update_id": 1, "message": {"text": "/status"}}

    rejected = await request(app, "POST", path, json=update)
    accepted = await request(
        app,
        "POST",
        path,
        json=update,
        headers={"x-telegram-bot-api-secret-token": settings.telegram_webhook_secret},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert commands.updates == [(update, True)]


async def test_job_endpoint_rejects_unsigned_request(web_setup):
    app, _, _, _, qstash, automation = web_setup
    qstash.error = ValueError("invalid")

    response = await request(
        app,
        "POST",
        "/jobs/maintain",
        content=b'{"wake":true}',
        headers={"upstash-signature": "bad"},
    )

    assert response.status_code == 403
    assert automation.maintains == 0


async def test_job_verification_uses_raw_body_and_repeated_publish_is_idempotent(web_setup):
    app, _, _, _, qstash, automation = web_setup
    headers = {"upstash-signature": "valid", "content-type": "application/json"}

    first = await request(
        app,
        "POST",
        "/jobs/publish?slot=14%3A00",
        content=b'{"wake":true}',
        headers=headers,
    )
    second = await request(
        app,
        "POST",
        "/jobs/publish?slot=14%3A00",
        content=b'{"wake":true}',
        headers=headers,
    )

    assert first.json()["state"] == "published"
    assert second.json()["state"] == "idle"
    assert automation.publishes == 2
    assert qstash.calls[0] == (
        b'{"wake":true}',
        "valid",
        "/jobs/publish?slot=14%3A00",
    )


async def test_retryable_job_error_returns_503_for_qstash_retry(web_setup):
    app, _, _, _, _, automation = web_setup
    automation.maintain_error = RetryableJobError("database unavailable")

    response = await request(
        app,
        "POST",
        "/jobs/maintain",
        content=b"{}",
        headers={"upstash-signature": "valid"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Временная ошибка задания"
