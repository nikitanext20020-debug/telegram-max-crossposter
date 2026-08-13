from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class ContentType(StrEnum):
    ANY = "any"
    VIDEO = "video"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class ScheduleRule:
    time: time
    content_type: ContentType


DEFAULT_SCHEDULE = (
    ScheduleRule(time(8, 0), ContentType.ANY),
    ScheduleRule(time(10, 0), ContentType.ANY),
    ScheduleRule(time(12, 0), ContentType.ANY),
    ScheduleRule(time(14, 0), ContentType.VIDEO),
    ScheduleRule(time(18, 0), ContentType.ANY),
    ScheduleRule(time(21, 0), ContentType.ANY),
)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Не задана обязательная переменная {name}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int | None = None) -> int:
    raw = env.get(name)
    if raw is None and default is not None:
        return default
    value = _required(env, name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} должен быть положительным целым числом") from error
    if parsed <= 0:
        raise ValueError(f"{name} должен быть положительным целым числом")
    return parsed


def _http_url(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    raw = env.get(name, default or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"{name} должен быть корректным http(s) URL")
    return raw


def _schedule(value: str | None) -> tuple[ScheduleRule, ...]:
    if value is None or not value.strip():
        return DEFAULT_SCHEDULE
    rules: list[ScheduleRule] = []
    try:
        for raw_rule in value.split(","):
            # Split from the right so HH:MM remains intact.
            time_part, type_part = raw_rule.strip().rsplit(":", 1)
            hour_text, minute_text = time_part.split(":", 1)
            parsed_time = time(int(hour_text), int(minute_text))
            rules.append(ScheduleRule(parsed_time, ContentType(type_part.strip())))
    except (TypeError, ValueError) as error:
        raise ValueError("DEFAULT_SCHEDULE должен иметь вид HH:MM:any,HH:MM:video") from error
    if not rules or len({rule.time for rule in rules}) != len(rules):
        raise ValueError("DEFAULT_SCHEDULE содержит пустое или повторяющееся время")
    return tuple(sorted(rules, key=lambda rule: rule.time))


def _database_url(env: Mapping[str, str]) -> str:
    raw = _required(env, "DATABASE_URL")
    parts = urlsplit(raw)
    if parts.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        return raw
    query = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name == "channel_binding":
            continue
        query.append(("ssl" if name == "sslmode" else name, value))
    return urlunsplit(
        ("postgresql+asyncpg", parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_string: str
    telegram_admin_bot_token: str
    telegram_webhook_secret: str
    owner_telegram_id: int
    database_url: str
    max_bot_token: str
    max_chat_id: str
    qstash_token: str
    qstash_current_signing_key: str
    qstash_next_signing_key: str
    public_url: str
    signature_text: str
    signature_url: str
    default_schedule: tuple[ScheduleRule, ...]
    timezone: str = "Europe/Moscow"
    queue_target: int = 12
    queue_low_water: int = 6
    max_api_base: str = "https://platform-api2.max.ru"

    @classmethod
    def from_env(cls, source: Mapping[str, str]) -> Settings:
        env = MappingProxyType(dict(source))
        webhook_secret = _required(env, "TELEGRAM_WEBHOOK_SECRET")
        if not all(character.isalnum() or character in "_-" for character in webhook_secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET содержит недопустимые символы")

        return cls(
            telegram_api_id=_positive_int(env, "TELEGRAM_API_ID"),
            telegram_api_hash=_required(env, "TELEGRAM_API_HASH"),
            telegram_session_string=_required(env, "TELEGRAM_SESSION_STRING"),
            telegram_admin_bot_token=_required(env, "TELEGRAM_ADMIN_BOT_TOKEN"),
            telegram_webhook_secret=webhook_secret,
            owner_telegram_id=_positive_int(env, "OWNER_TELEGRAM_ID", 8235497168),
            database_url=_database_url(env),
            max_bot_token=_required(env, "MAX_BOT_TOKEN"),
            max_chat_id=_required(env, "MAX_CHAT_ID"),
            qstash_token=_required(env, "QSTASH_TOKEN"),
            qstash_current_signing_key=_required(env, "QSTASH_CURRENT_SIGNING_KEY"),
            qstash_next_signing_key=_required(env, "QSTASH_NEXT_SIGNING_KEY"),
            public_url=_http_url(env, "PUBLIC_URL"),
            signature_text=env.get("MAX_SIGNATURE_TEXT", "НАШ ТГК").strip() or "НАШ ТГК",
            signature_url=_http_url(env, "MAX_SIGNATURE_URL", "https://t.me/webm4ik"),
            default_schedule=_schedule(env.get("DEFAULT_SCHEDULE")),
        )
