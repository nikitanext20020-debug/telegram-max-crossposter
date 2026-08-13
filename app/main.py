from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.bot_api import BotApi
from app.commands import CommandService
from app.config import ContentType, ScheduleRule, Settings
from app.database import Database
from app.max_client import MaxClient
from app.publisher import Publisher
from app.qstash import QStashService
from app.queueing import QueuePlanner
from app.store import Store
from app.telegram_reader import ScanWindow, TelegramReader


class RetryableJobError(RuntimeError):
    pass


class AutomationService:
    def __init__(self, store, reader, planner, publisher):
        self.store = store
        self.reader = reader
        self.planner = planner
        self.publisher = publisher

    async def maintain(self) -> dict:
        lock_token = await self.store.acquire_job_lock(
            "content-maintenance", datetime.now(UTC), lease_seconds=10 * 60
        )
        if lock_token is None:
            return {"locked": True, "scanned": 0, "queued": 0}
        try:
            sources = await self.store.list_sources(enabled_only=True)
            if not sources:
                source = await self.reader.add_source("https://t.me/animeworldmem")
                sources = [source]
            scanned = inserted = duplicates = 0
            now = datetime.now(UTC)
            for source in sources:
                if source.retry_after and source.retry_after > now:
                    continue
                result = await self.reader.scan_source(source, ScanWindow(limit=100))
                scanned += result.scanned_messages
                inserted += result.inserted_candidates
                duplicates += result.duplicates
            refill = await self.planner.refill(now)
            return {
                "scanned": scanned,
                "inserted": inserted,
                "duplicates": duplicates,
                "queued": refill.added,
                "unfilled": refill.unfilled,
            }
        except Exception as error:
            raise RetryableJobError("Не удалось обновить очередь") from error
        finally:
            await self.store.release_job_lock("content-maintenance", lock_token)

    async def publish(self):
        try:
            outcome = await self.publisher.publish_due(datetime.now(UTC))
            if outcome.state != "idle":
                await self.maintain()
            return outcome
        except RetryableJobError:
            raise
        except Exception as error:
            raise RetryableJobError("Не удалось выполнить публикацию") from error


@dataclass(slots=True)
class AppServices:
    database: object
    commands: object
    qstash: object
    automation: object
    store: object | None = None
    bot: object | None = None
    max_client: object | None = None
    owned: bool = False
    schedule_sync: object | None = None


def _schedule_rules(rows) -> list[ScheduleRule]:
    return [
        ScheduleRule(
            datetime.strptime(row.local_time, "%H:%M").time(),
            ContentType(row.content_type),
        )
        for row in rows
    ]


def build_services(settings: Settings) -> AppServices:
    database = Database(settings.database_url)
    store = Store(database)
    bot = BotApi(settings.telegram_admin_bot_token)
    reader = TelegramReader(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_string=settings.telegram_session_string,
        store=store,
    )
    planner = QueuePlanner(store)
    max_client = MaxClient(
        token=settings.max_bot_token,
        chat_id=settings.max_chat_id,
        api_base=settings.max_api_base,
    )
    publisher = Publisher(store, reader, max_client)
    qstash = QStashService(
        token=settings.qstash_token,
        current_signing_key=settings.qstash_current_signing_key,
        next_signing_key=settings.qstash_next_signing_key,
        public_url=settings.public_url,
    )

    async def sync_schedule() -> None:
        rows = await store.list_schedule()
        await qstash.reconcile(_schedule_rules(rows))

    commands = CommandService(
        owner_id=settings.owner_telegram_id,
        store=store,
        reader=reader,
        planner=planner,
        publisher=publisher,
        bot=bot,
        schedule_sync=sync_schedule,
    )
    automation = AutomationService(store, reader, planner, publisher)
    return AppServices(
        database=database,
        commands=commands,
        qstash=qstash,
        automation=automation,
        store=store,
        bot=bot,
        max_client=max_client,
        owned=True,
        schedule_sync=sync_schedule,
    )


def create_app(settings: Settings, *, services: AppServices | None = None) -> FastAPI:
    container = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(_app):
        if container.owned:
            await container.database.start()
            await container.store.ensure_settings(
                owner_telegram_id=settings.owner_telegram_id,
                timezone=settings.timezone,
                signature_text=settings.signature_text,
                signature_url=settings.signature_url,
                max_chat_id=settings.max_chat_id,
                queue_target=settings.queue_target,
                queue_low_water=settings.queue_low_water,
            )
            if not await container.store.list_schedule():
                await container.store.replace_schedule(list(settings.default_schedule))
            await container.schedule_sync()
        try:
            yield
        finally:
            if container.owned:
                await container.max_client.close()
                await container.bot.close()
                await container.database.close()

    app = FastAPI(title="Telegram MAX publisher", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        healthy = await container.database.healthy()
        return JSONResponse(
            {"ok": healthy, "database": healthy}, status_code=200 if healthy else 503
        )

    @app.post("/telegram/webhook/{path_secret}")
    async def telegram_webhook(
        path_secret: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        header_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not (
            secrets.compare_digest(path_secret, settings.telegram_webhook_secret)
            and secrets.compare_digest(header_secret, settings.telegram_webhook_secret)
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="Payload too large")
        try:
            update = await request.json()
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid JSON") from error
        await container.commands.handle_update(update, background=background_tasks)
        return {"ok": True}

    def verified_body(request: Request, body: bytes) -> None:
        raw_query = request.scope.get("query_string", b"").decode()
        path_and_query = request.url.path + (f"?{raw_query}" if raw_query else "")
        try:
            container.qstash.verify(
                body=body,
                signature=request.headers.get("upstash-signature", ""),
                path_and_query=path_and_query,
            )
        except Exception as error:
            raise HTTPException(status_code=403, detail="Forbidden") from error

    @app.post("/jobs/maintain")
    async def maintain(request: Request):
        body = await request.body()
        verified_body(request, body)
        try:
            result = await container.automation.maintain()
            return {"ok": True, **result}
        except RetryableJobError as error:
            raise HTTPException(status_code=503, detail="Временная ошибка задания") from error

    @app.post("/jobs/publish")
    async def publish(request: Request, slot: str):
        del slot
        body = await request.body()
        verified_body(request, body)
        try:
            outcome = await container.automation.publish()
            return {"ok": True, "state": outcome.state}
        except RetryableJobError as error:
            raise HTTPException(status_code=503, detail="Временная ошибка задания") from error

    return app


def create_app_from_env() -> FastAPI:
    return create_app(Settings.from_env(os.environ))
