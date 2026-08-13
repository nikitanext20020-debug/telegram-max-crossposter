from datetime import time

import pytest

from app.config import ContentType, Settings


@pytest.fixture
def complete_env():
    return {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "hash-value",
        "TELEGRAM_SESSION_STRING": "session-value",
        "TELEGRAM_ADMIN_BOT_TOKEN": "bot-token",
        "TELEGRAM_WEBHOOK_SECRET": "safe_webhook-secret_123",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@example.test/db",
        "MAX_BOT_TOKEN": "max-token",
        "MAX_CHAT_ID": "-987654321",
        "QSTASH_TOKEN": "qstash-token",
        "QSTASH_CURRENT_SIGNING_KEY": "current-key",
        "QSTASH_NEXT_SIGNING_KEY": "next-key",
        "PUBLIC_URL": "https://crossposter.example.test/",
    }


def test_defaults_owner_signature_and_schedule(complete_env):
    settings = Settings.from_env(complete_env)

    assert settings.owner_telegram_id == 8235497168
    assert settings.signature_text == "НАШ ТГК"
    assert settings.signature_url == "https://t.me/webm4ik"
    assert settings.public_url == "https://crossposter.example.test"
    assert [
        (rule.time.isoformat(timespec="minutes"), rule.content_type.value)
        for rule in settings.default_schedule
    ] == [
        ("08:00", "any"),
        ("10:00", "any"),
        ("12:00", "any"),
        ("14:00", "video"),
        ("18:00", "any"),
        ("21:00", "any"),
    ]


def test_custom_schedule_and_owner_parse_strictly(complete_env):
    complete_env["OWNER_TELEGRAM_ID"] = "42"
    complete_env["DEFAULT_SCHEDULE"] = "09:15:image,14:30:video"

    settings = Settings.from_env(complete_env)

    assert settings.owner_telegram_id == 42
    assert settings.default_schedule[0].time == time(9, 15)
    assert settings.default_schedule[0].content_type is ContentType.IMAGE
    assert settings.default_schedule[1].content_type is ContentType.VIDEO


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("TELEGRAM_API_ID", "not-a-number", "TELEGRAM_API_ID"),
        ("OWNER_TELEGRAM_ID", "0", "OWNER_TELEGRAM_ID"),
        ("PUBLIC_URL", "ftp://example.test", "PUBLIC_URL"),
        ("DEFAULT_SCHEDULE", "25:00:any", "DEFAULT_SCHEDULE"),
        ("DEFAULT_SCHEDULE", "10:00:audio", "DEFAULT_SCHEDULE"),
    ],
)
def test_rejects_malformed_configuration(complete_env, name, value, message):
    complete_env[name] = value

    with pytest.raises(ValueError, match=message):
        Settings.from_env(complete_env)


def test_missing_secret_names_variable_without_echoing_other_values(complete_env):
    complete_env.pop("MAX_BOT_TOKEN")

    with pytest.raises(ValueError) as raised:
        Settings.from_env(complete_env)

    assert str(raised.value) == "Не задана обязательная переменная MAX_BOT_TOKEN"
    assert "session-value" not in str(raised.value)


def test_normalizes_neon_url_for_asyncpg(complete_env):
    complete_env["DATABASE_URL"] = (
        "postgresql://user:pass@host/db?sslmode=require&channel_binding=require"
    )
    settings = Settings.from_env(complete_env)
    assert settings.database_url == ("postgresql+asyncpg://user:pass@host/db?ssl=require")
