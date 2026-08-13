from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

from app.config import ContentType, ScheduleRule
from app.telegram_reader import ScanWindow
from app.text import build_signature_html


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class HandleResult:
    accepted: bool = False
    ignored: str | None = None


class CommandParser:
    ALIASES: ClassVar = {"build": "parse", "start": "help"}

    @classmethod
    def parse(cls, text: str) -> Command:
        value = text.strip()
        if re.match(r"(?i)^(?:https?://)?(?:t\.me|telegram\.me)/", value):
            return Command("add_source", value)
        if not value.startswith("/"):
            raise ValueError("Отправьте команду /help или ссылку t.me на канал")
        head, _, arguments = value.partition(" ")
        name = head[1:].split("@", 1)[0].casefold()
        name = cls.ALIASES.get(name, name)
        supported = {
            "help",
            "status",
            "add_source",
            "sources",
            "del_source",
            "parse",
            "parse_period",
            "parse_from",
            "parse_top",
            "queue",
            "refresh",
            "now",
            "times",
            "slot",
            "signature",
            "signature_reset",
        }
        if name not in supported:
            raise ValueError("Неизвестная команда. Используйте /help")
        return Command(name, arguments.strip())


HELP_TEXT = """Команды управления:
/status — состояние сервиса
/add_source <ссылка> — добавить источник
/sources — список источников
/del_source <номер|ссылка> — отключить источник
/parse [количество] — наполнить очередь
/parse_period <7d|12h> [количество]
/parse_from <ДД.ММ.ГГГГ> [количество]
/parse_top <7d> [количество] — самые активные
/queue [количество] — очередь
/refresh — перестроить неопубликованную очередь
/now <ссылка на пост> — опубликовать сразу
/times [08:00,10:00,...] — расписание
/slot <время> <any|video|image>
/signature [текст | ссылка] — подпись MAX
/signature_reset — вернуть НАШ ТГК"""


def _positive_count(value: str, default: int = 100) -> int:
    if not value:
        return default
    try:
        count = int(value)
    except ValueError as error:
        raise ValueError("Количество должно быть целым числом") from error
    if not 1 <= count <= 5000:
        raise ValueError("Количество должно быть от 1 до 5000")
    return count


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dh])", value.casefold())
    if not match:
        raise ValueError("Период должен иметь вид 7d или 12h")
    amount = int(match.group(1))
    if not 1 <= amount <= 8760:
        raise ValueError("Период вне допустимого диапазона")
    return timedelta(days=amount) if match.group(2) == "d" else timedelta(hours=amount)


def _parse_time(value: str) -> time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as error:
        raise ValueError("Время должно иметь вид ЧЧ:ММ") from error
    return parsed


class CommandService:
    def __init__(
        self,
        *,
        owner_id: int,
        store,
        reader,
        planner,
        publisher,
        bot,
        schedule_sync=None,
    ):
        self.owner_id = owner_id
        self.store = store
        self.reader = reader
        self.planner = planner
        self.publisher = publisher
        self.bot = bot
        self.schedule_sync = schedule_sync

    async def handle_update(self, update: dict, *, background=None) -> HandleResult:
        message = update.get("message") or update.get("edited_message")
        if not message or not isinstance(message.get("text"), str):
            return HandleResult(ignored="not_text")
        sender_id = (message.get("from") or {}).get("id")
        if sender_id != self.owner_id:
            return HandleResult(ignored="unauthorized")
        chat_id = int((message.get("chat") or {}).get("id", sender_id))
        try:
            command = CommandParser.parse(message["text"])
        except ValueError as error:
            await self.bot.send_message(chat_id, f"Ошибка: {error}")
            return HandleResult(accepted=True)

        long_commands = {
            "add_source": "Добавляю источник и наполняю очередь…",
            "parse": "Начинаю сбор постов…",
            "parse_period": "Начинаю сбор постов за период…",
            "parse_from": "Начинаю сбор постов с указанной даты…",
            "parse_top": "Ищу самые активные посты…",
            "refresh": "Перестраиваю неопубликованную очередь…",
            "now": "Публикую пост сразу…",
        }
        if command.name in long_commands:
            await self.bot.send_message(chat_id, long_commands[command.name])
            if background is not None:
                background.add_task(self._safe_execute, command, chat_id)
            else:
                await self._safe_execute(command, chat_id)
        else:
            await self._safe_execute(command, chat_id)
        return HandleResult(accepted=True)

    async def _safe_execute(self, command: Command, chat_id: int) -> None:
        try:
            await self._execute(command, chat_id)
        except Exception as error:
            await self.bot.send_message(chat_id, f"Ошибка: {error}")

    async def _execute(self, command: Command, chat_id: int) -> None:
        handler = getattr(self, f"_command_{command.name}")
        await handler(command.arguments, chat_id)

    async def _command_help(self, _arguments: str, chat_id: int) -> None:
        await self.bot.send_message(chat_id, HELP_TEXT)

    async def _command_status(self, _arguments: str, chat_id: int) -> None:
        healthy = await self.store.database.healthy()
        sources = await self.store.list_sources(enabled_only=True)
        queue = await self.store.list_queue()
        pending = sum(row.state == "pending" for row in queue)
        await self.bot.send_message(
            chat_id,
            f"База: {'работает' if healthy else 'ошибка'}\n"
            f"Источников: {len(sources)}\nВ очереди: {pending}",
        )

    async def _command_add_source(self, arguments: str, chat_id: int) -> None:
        if not arguments:
            raise ValueError("Укажите ссылку на Telegram-канал")
        source = await self.reader.add_source(arguments)
        scan = await self.reader.scan_source(source, ScanWindow(limit=100, use_cursor=False))
        refill = await self.planner.refill(datetime.now(UTC))
        await self.bot.send_message(
            chat_id,
            f"Источник добавлен: {source.title or source.link}\n"
            f"Найдено новых: {scan.inserted_candidates}\n"
            f"Добавлено в очередь: {refill.added}",
        )

    async def _command_sources(self, _arguments: str, chat_id: int) -> None:
        sources = await self.store.list_sources()
        if not sources:
            await self.bot.send_message(chat_id, "Источников пока нет")
            return
        lines = [
            f"{source.id}. {source.title or source.link} — "
            f"{'включён' if source.enabled else 'отключён'} ({source.join_state})"
            for source in sources
        ]
        await self.bot.send_message(chat_id, "\n".join(lines))

    async def _command_del_source(self, arguments: str, chat_id: int) -> None:
        if not arguments:
            raise ValueError("Укажите номер или ссылку источника")
        sources = await self.store.list_sources()
        selected = next(
            (
                source
                for source in sources
                if str(source.id) == arguments or source.link == arguments
            ),
            None,
        )
        if selected is None:
            raise ValueError("Источник не найден")
        await self.store.disable_source(selected.id)
        await self.planner.refresh(datetime.now(UTC))
        await self.bot.send_message(chat_id, "Источник отключён, очередь обновлена")

    async def _scan(self, arguments: str, mode: str, chat_id: int) -> None:
        parts = arguments.split()
        now = datetime.now(UTC)
        since = None
        count = 100
        top_mode = mode == "parse_top"
        if mode == "parse":
            count = _positive_count(parts[0] if parts else "")
        elif mode in {"parse_period", "parse_top"}:
            if not parts:
                raise ValueError("Укажите период, например 7d")
            since = now - _duration(parts[0])
            count = _positive_count(parts[1] if len(parts) > 1 else "")
        elif mode == "parse_from":
            if not parts:
                raise ValueError("Укажите дату ДД.ММ.ГГГГ")
            try:
                local = datetime.strptime(parts[0], "%d.%m.%Y").replace(
                    tzinfo=ZoneInfo("Europe/Moscow")
                )
            except ValueError as error:
                raise ValueError("Дата должна иметь вид ДД.ММ.ГГГГ") from error
            since = local.astimezone(UTC)
            count = _positive_count(parts[1] if len(parts) > 1 else "", 500)

        sources = await self.store.list_sources(enabled_only=True)
        if not sources:
            raise ValueError("Сначала добавьте хотя бы один источник")
        scanned = inserted = duplicates = 0
        for source in sources:
            result = await self.reader.scan_source(
                source,
                ScanWindow(limit=count, since=since, use_cursor=False),
            )
            scanned += result.scanned_messages
            inserted += result.inserted_candidates
            duplicates += result.duplicates
        refill = await self.planner.refill(now, top_mode=top_mode)
        await self.bot.send_message(
            chat_id,
            f"Проверено сообщений: {scanned}\n"
            f"Добавлено кандидатов: {inserted}\n"
            f"Дубликатов: {duplicates}\n"
            f"Поставлено в очередь: {refill.added}",
        )

    async def _command_parse(self, arguments: str, chat_id: int) -> None:
        await self._scan(arguments, "parse", chat_id)

    async def _command_parse_period(self, arguments: str, chat_id: int) -> None:
        await self._scan(arguments, "parse_period", chat_id)

    async def _command_parse_from(self, arguments: str, chat_id: int) -> None:
        await self._scan(arguments, "parse_from", chat_id)

    async def _command_parse_top(self, arguments: str, chat_id: int) -> None:
        await self._scan(arguments, "parse_top", chat_id)

    async def _command_queue(self, arguments: str, chat_id: int) -> None:
        count = _positive_count(arguments, 20)
        count = min(count, 50)
        rows = (await self.store.list_queue())[:count]
        if not rows:
            await self.bot.send_message(chat_id, "Очередь пуста")
            return
        lines = [
            f"{row.scheduled_at} — {row.media_kind} — {row.source_title or row.source_id} "
            f"— {row.state}"
            for row in rows
        ]
        await self.bot.send_message(chat_id, "\n".join(lines))

    async def _command_refresh(self, _arguments: str, chat_id: int) -> None:
        result = await self.planner.refresh(datetime.now(UTC))
        await self.bot.send_message(chat_id, f"Очередь перестроена. Добавлено: {result.added}")

    async def _command_now(self, arguments: str, chat_id: int) -> None:
        if not arguments:
            raise ValueError("Укажите ссылку на Telegram-пост")
        result = await self.publisher.publish_now(arguments)
        messages = {
            "published": "Опубликовано сразу",
            "skipped_duplicate": "Этот пост уже публиковался",
            "ambiguous": "MAX мог опубликовать пост, автоматический повтор запрещён",
            "failed": f"Не удалось опубликовать: {result.error or 'ошибка'}",
        }
        await self.bot.send_message(chat_id, messages.get(result.state, result.state))

    async def _command_times(self, arguments: str, chat_id: int) -> None:
        existing = await self.store.list_schedule()
        if not arguments:
            await self.bot.send_message(
                chat_id,
                "\n".join(f"{row.local_time} — {row.content_type}" for row in existing),
            )
            return
        values = [value.strip() for value in arguments.split(",") if value.strip()]
        if not values or len(values) > 9:
            raise ValueError("Можно указать от 1 до 9 времён")
        parsed = [_parse_time(value) for value in values]
        if len(set(parsed)) != len(parsed):
            raise ValueError("В расписании повторяется время")
        old_types = {row.local_time: ContentType(row.content_type) for row in existing}
        rules = [
            ScheduleRule(
                value,
                old_types.get(value.isoformat(timespec="minutes"), ContentType.ANY),
            )
            for value in parsed
        ]
        await self.store.replace_schedule(rules)
        if self.schedule_sync:
            await self.schedule_sync()
        await self.planner.refresh(datetime.now(UTC))
        await self.bot.send_message(chat_id, "Расписание обновлено")

    async def _command_slot(self, arguments: str, chat_id: int) -> None:
        parts = arguments.split()
        if len(parts) != 2:
            raise ValueError("Используйте /slot ЧЧ:ММ any|video|image")
        slot_time = _parse_time(parts[0])
        try:
            content_type = ContentType(parts[1].casefold())
        except ValueError as error:
            raise ValueError("Тип должен быть any, video или image") from error
        rows = await self.store.list_schedule()
        mapping = {row.local_time: ContentType(row.content_type) for row in rows}
        mapping[slot_time.isoformat(timespec="minutes")] = content_type
        if len(mapping) > 9:
            raise ValueError("В бесплатном QStash доступно не больше 9 времён")
        rules = [ScheduleRule(_parse_time(raw_time), rule) for raw_time, rule in mapping.items()]
        await self.store.replace_schedule(rules)
        if self.schedule_sync:
            await self.schedule_sync()
        await self.planner.refresh(datetime.now(UTC))
        await self.bot.send_message(chat_id, "Правило слота обновлено")

    async def _command_signature(self, arguments: str, chat_id: int) -> None:
        if not arguments:
            settings = await self.store.get_settings()
            await self.bot.send_message(
                chat_id,
                f"Текст: {settings.signature_text}\nСсылка: {settings.signature_url}",
            )
            return
        if "|" not in arguments:
            raise ValueError("Используйте /signature ТЕКСТ | https://ссылка")
        text, url = (value.strip() for value in arguments.split("|", 1))
        build_signature_html(text, url)
        await self.store.update_signature(text, url)
        await self.bot.send_message(chat_id, "Подпись обновлена")

    async def _command_signature_reset(self, _arguments: str, chat_id: int) -> None:
        await self.store.update_signature("НАШ ТГК", "https://t.me/webm4ik")
        await self.bot.send_message(chat_id, "Подпись восстановлена: НАШ ТГК")
