from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from app.bot_api import BotApi
from app.config import ContentType, ScheduleRule, Settings
from app.database import Database
from app.max_client import MaxClient
from app.qstash import QStashService
from app.store import Store
from app.telegram_reader import TelegramReader


@dataclass(slots=True)
class CliDependencies:
    settings: object
    database: object
    store: object
    reader: object
    max_client: object
    bot: object
    qstash: object
    owned: bool = False


def build_dependencies(settings: Settings) -> CliDependencies:
    database = Database(settings.database_url)
    store = Store(database)
    reader = TelegramReader(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_string=settings.telegram_session_string,
        store=store,
    )
    max_client = MaxClient(
        token=settings.max_bot_token,
        chat_id=settings.max_chat_id,
        api_base=settings.max_api_base,
    )
    bot = BotApi(settings.telegram_admin_bot_token)
    qstash = QStashService(
        token=settings.qstash_token,
        current_signing_key=settings.qstash_current_signing_key,
        next_signing_key=settings.qstash_next_signing_key,
        public_url=settings.public_url,
    )
    return CliDependencies(
        settings=settings,
        database=database,
        store=store,
        reader=reader,
        max_client=max_client,
        bot=bot,
        qstash=qstash,
        owned=True,
    )


def build_command_dependencies(command: str, env: Mapping[str, str]) -> CliDependencies:
    if command == "discover-max":
        max_client = MaxClient(
            token=_required(env, "MAX_BOT_TOKEN"),
            chat_id=env.get("MAX_CHAT_ID", "discovery"),
            api_base=env.get("MAX_API_BASE", "https://platform-api2.max.ru"),
        )
        return CliDependencies(
            settings=SimpleNamespace(),
            database=None,
            store=None,
            reader=None,
            max_client=max_client,
            bot=None,
            qstash=None,
            owned=True,
        )
    if command == "verify-session":
        reader = TelegramReader(
            api_id=int(_required(env, "TELEGRAM_API_ID")),
            api_hash=_required(env, "TELEGRAM_API_HASH"),
            session_string=_required(env, "TELEGRAM_SESSION_STRING"),
            store=None,
        )
        return CliDependencies(
            settings=SimpleNamespace(),
            database=None,
            store=None,
            reader=reader,
            max_client=None,
            bot=None,
            qstash=None,
            owned=True,
        )
    if command == "register-webhook":
        settings = SimpleNamespace(
            public_url=_required(env, "PUBLIC_URL").rstrip("/"),
            telegram_webhook_secret=_required(env, "TELEGRAM_WEBHOOK_SECRET"),
        )
        return CliDependencies(
            settings=settings,
            database=None,
            store=None,
            reader=None,
            max_client=None,
            bot=BotApi(_required(env, "TELEGRAM_ADMIN_BOT_TOKEN")),
            qstash=None,
            owned=True,
        )
    return build_dependencies(Settings.from_env(env))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m app.cli")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    commands.add_parser("session")
    commands.add_parser("verify-session")
    commands.add_parser("discover-max")
    commands.add_parser("register-webhook")
    commands.add_parser("sync-schedules")
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--publish", action="store_true")
    return root


def generate_string_session(api_id: int, api_hash: str) -> str:
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        return client.session.save()


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Не задана обязательная переменная {name}")
    return value


def _rules(rows) -> list[ScheduleRule]:
    from datetime import datetime

    return [
        ScheduleRule(
            datetime.strptime(row.local_time, "%H:%M").time(),
            ContentType(row.content_type),
        )
        for row in rows
    ]


async def _close_dependencies(dependencies: CliDependencies) -> None:
    if dependencies.max_client is not None:
        await dependencies.max_client.close()
    if dependencies.bot is not None:
        await dependencies.bot.close()
    if dependencies.database is not None:
        await dependencies.database.close()


async def run_async(
    argv: list[str],
    *,
    env: Mapping[str, str] | None = None,
    dependencies: CliDependencies | None = None,
    output: Callable[[str], None] = print,
) -> int:
    arguments = parser().parse_args(argv)
    source_env = os.environ if env is None else env
    if arguments.command == "session":
        try:
            api_id = int(_required(source_env, "TELEGRAM_API_ID"))
            api_hash = _required(source_env, "TELEGRAM_API_HASH")
            session = await asyncio.to_thread(generate_string_session, api_id, api_hash)
        except Exception as error:
            output(f"Ошибка создания Telegram-сеанса: {error}")
            return 1
        output("Сохраните следующую строку только в TELEGRAM_SESSION_STRING:")
        output(session)
        output("Не отправляйте её в чат и не добавляйте в Git.")
        return 0

    owned = dependencies is None
    try:
        if dependencies is None:
            dependencies = build_command_dependencies(arguments.command, source_env)
        settings = dependencies.settings

        if arguments.command == "init-db":
            await dependencies.database.start()
            await dependencies.store.ensure_settings(
                owner_telegram_id=settings.owner_telegram_id,
                timezone=settings.timezone,
                signature_text=settings.signature_text,
                signature_url=settings.signature_url,
                max_chat_id=settings.max_chat_id,
                queue_target=settings.queue_target,
                queue_low_water=settings.queue_low_water,
            )
            if not await dependencies.store.list_schedule():
                await dependencies.store.replace_schedule(list(settings.default_schedule))
            output("База и постоянные настройки инициализированы.")
        elif arguments.command == "verify-session":
            account_id = await dependencies.reader.verify_session()
            output(f"Telegram account ID: {account_id}")
        elif arguments.command == "discover-max":
            me = await dependencies.max_client.get("/me")
            output(f"MAX-бот: {me.get('name') or me.get('username') or me.get('user_id')}")
            data = await dependencies.max_client.get(
                "/updates?timeout=0&types=bot_added,message_created,chat_title_changed"
            )
            channels = [
                item
                for item in data.get("updates", [])
                if item.get("is_channel") is True and item.get("chat_id")
            ]
            if not channels:
                output("MAX-канал не найден. Перевыдайте событие добавления бота и повторите.")
                return 2
            for item in channels:
                title = item.get("title") or item.get("chat_title") or "MAX channel"
                output(f"Канал: {title}")
                output(f"MAX_CHAT_ID={item['chat_id']}")
        elif arguments.command == "register-webhook":
            await dependencies.bot.call(
                "setWebhook",
                {
                    "url": (
                        f"{settings.public_url}/telegram/webhook/{settings.telegram_webhook_secret}"
                    ),
                    "secret_token": settings.telegram_webhook_secret,
                    "allowed_updates": ["message", "edited_message"],
                    "drop_pending_updates": False,
                    "max_connections": 5,
                },
            )
            output("Webhook Telegram-админбота подключён.")
        elif arguments.command == "sync-schedules":
            rows = await dependencies.store.list_schedule()
            schedule_ids = await dependencies.qstash.reconcile(_rules(rows))
            output(f"Синхронизировано {len(schedule_ids)} расписаний QStash.")
        elif arguments.command == "smoke":
            if not await dependencies.database.healthy():
                raise RuntimeError("База данных недоступна")
            account_id = await dependencies.reader.verify_session()
            me = await dependencies.max_client.get("/me")
            output(
                f"База: OK; Telegram account: {account_id}; "
                f"MAX bot: {me.get('name') or me.get('user_id')}"
            )
            if arguments.publish:
                message_id = await dependencies.max_client.send(
                    '[SMOKE]\n\n<a href="https://t.me/webm4ik">НАШ ТГК</a>', []
                )
                output(f"Тестовая публикация MAX: {message_id}")
            else:
                output("Публикация не выполнялась; добавьте --publish для явного теста.")
        return 0
    except Exception as error:
        output(f"Ошибка: {error}")
        return 1
    finally:
        if owned and dependencies is not None:
            await _close_dependencies(dependencies)


def main() -> None:
    raise SystemExit(asyncio.run(run_async(os.sys.argv[1:])))


if __name__ == "__main__":
    main()
