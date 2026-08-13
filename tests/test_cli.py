from types import SimpleNamespace

import pytest

from app.cli import CliDependencies, run_async


class FakeDatabase:
    def __init__(self):
        self.started = 0

    async def start(self):
        self.started += 1

    async def healthy(self):
        return True

    async def close(self):
        return None


class FakeStore:
    def __init__(self):
        self.ensured = 0
        self.replaced = 0

    async def ensure_settings(self, **kwargs):
        self.ensured += 1

    async def list_schedule(self):
        return [SimpleNamespace(local_time="14:00", content_type="video")]

    async def replace_schedule(self, rules):
        self.replaced += 1


class FakeReader:
    async def verify_session(self):
        return 8235497168


class FakeMax:
    def __init__(self):
        self.sent = []

    async def get(self, path):
        if path == "/me":
            return {"name": "MAX bot", "user_id": 5}
        assert path.startswith("/updates")
        return {
            "updates": [
                {"chat_id": "-old", "is_channel": False},
                {
                    "chat_id": "-777",
                    "is_channel": True,
                    "title": "channel_animenaruto",
                },
            ]
        }

    async def send(self, html, attachments):
        self.sent.append((html, attachments))
        return "max-smoke"

    async def close(self):
        return None


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call(self, method, payload):
        self.calls.append((method, payload))
        return True

    async def close(self):
        return None


class FakeQStash:
    def __init__(self):
        self.rules = []

    async def reconcile(self, rules):
        self.rules = rules
        return {"tgmax-maintain", "tgmax-publish-1400"}


@pytest.fixture
def cli_dependencies():
    settings = SimpleNamespace(
        telegram_session_string="super-secret-session",
        telegram_webhook_secret="webhook-secret",
        public_url="https://service.example",
        owner_telegram_id=8235497168,
        timezone="Europe/Moscow",
        signature_text="НАШ ТГК",
        signature_url="https://t.me/webm4ik",
        max_chat_id="-777",
        queue_target=12,
        queue_low_water=6,
        default_schedule=(),
    )
    return CliDependencies(
        settings=settings,
        database=FakeDatabase(),
        store=FakeStore(),
        reader=FakeReader(),
        max_client=FakeMax(),
        bot=FakeBot(),
        qstash=FakeQStash(),
    )


async def invoke(dependencies, *args):
    output = []
    code = await run_async(list(args), dependencies=dependencies, output=output.append)
    return code, "\n".join(output)


async def test_verify_session_prints_only_account_id(cli_dependencies):
    code, output = await invoke(cli_dependencies, "verify-session")
    assert code == 0
    assert "8235497168" in output
    assert "super-secret-session" not in output


async def test_discover_max_lists_channel_id_without_tokens(cli_dependencies):
    code, output = await invoke(cli_dependencies, "discover-max")
    assert code == 0
    assert "channel_animenaruto" in output
    assert "MAX_CHAT_ID=-777" in output
    assert "super-secret" not in output


async def test_register_webhook_uses_admin_bot_and_secret_header(cli_dependencies):
    code, output = await invoke(cli_dependencies, "register-webhook")
    method, payload = cli_dependencies.bot.calls[0]

    assert code == 0
    assert method == "setWebhook"
    assert payload == {
        "url": "https://service.example/telegram/webhook/webhook-secret",
        "secret_token": "webhook-secret",
        "allowed_updates": ["message", "edited_message"],
        "drop_pending_updates": False,
        "max_connections": 5,
    }
    assert "webhook-secret" not in output


async def test_smoke_never_publishes_without_explicit_flag(cli_dependencies):
    code, output = await invoke(cli_dependencies, "smoke")
    assert code == 0
    assert "Публикация не выполнялась" in output
    assert cli_dependencies.max_client.sent == []

    code, output = await invoke(cli_dependencies, "smoke", "--publish")
    assert code == 0
    assert cli_dependencies.max_client.sent
    assert "max-smoke" in output


async def test_init_db_and_schedule_sync_use_persistent_defaults(cli_dependencies):
    code, _ = await invoke(cli_dependencies, "init-db")
    assert code == 0
    assert cli_dependencies.database.started == 1
    assert cli_dependencies.store.ensured == 1

    code, output = await invoke(cli_dependencies, "sync-schedules")
    assert code == 0
    assert len(cli_dependencies.qstash.rules) == 1
    assert "2 расписаний" in output
