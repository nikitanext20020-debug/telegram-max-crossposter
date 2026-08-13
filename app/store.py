from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import ContentType, ScheduleRule
from app.database import Database
from app.domain import CandidateInput, telegram_media_identity
from app.models import Candidate, MediaHash, QueueItem, ScheduleSlot, SettingsRow, Source


@dataclass(frozen=True, slots=True)
class CandidateInsertResult:
    candidate_id: int
    inserted: bool
    duplicate_reason: str | None = None


@dataclass(frozen=True, slots=True)
class QueueView:
    id: int
    candidate_id: int
    scheduled_at: datetime
    slot_rule: str
    state: str
    source_id: int
    source_title: str
    source_post_key: str
    media_kind: str


class Store:
    def __init__(self, database: Database):
        self.database = database

    async def ensure_settings(
        self,
        *,
        owner_telegram_id: int,
        timezone: str,
        signature_text: str,
        signature_url: str,
        max_chat_id: str,
        queue_target: int,
        queue_low_water: int,
    ) -> SettingsRow:
        async with self.database.session() as session, session.begin():
            row = await session.get(SettingsRow, 1)
            if row is None:
                row = SettingsRow(
                    id=1,
                    owner_telegram_id=owner_telegram_id,
                    timezone=timezone,
                    signature_text=signature_text,
                    signature_url=signature_url,
                    max_chat_id=max_chat_id,
                    queue_target=queue_target,
                    queue_low_water=queue_low_water,
                )
                session.add(row)
            return row

    async def get_settings(self) -> SettingsRow:
        async with self.database.session() as session:
            row = await session.get(SettingsRow, 1)
            if row is None:
                raise RuntimeError("Настройки базы данных не инициализированы")
            return row

    async def update_signature(self, text: str, url: str) -> None:
        async with self.database.session() as session, session.begin():
            await session.execute(
                update(SettingsRow)
                .where(SettingsRow.id == 1)
                .values(signature_text=text, signature_url=url)
            )

    async def replace_schedule(self, rules: list[ScheduleRule]) -> None:
        async with self.database.session() as session, session.begin():
            await session.execute(delete(ScheduleSlot))
            session.add_all(
                ScheduleSlot(
                    local_time=rule.time.isoformat(timespec="minutes"),
                    content_type=rule.content_type.value,
                    enabled=True,
                )
                for rule in rules
            )

    async def list_schedule(self) -> list[ScheduleSlot]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(ScheduleSlot)
                .where(ScheduleSlot.enabled.is_(True))
                .order_by(ScheduleSlot.local_time)
            )
            return list(result)

    async def add_source(
        self,
        link: str,
        *,
        title: str = "",
        telegram_chat_id: int | None = None,
        join_state: str = "new",
    ) -> Source:
        async with self.database.session() as session, session.begin():
            row = await session.scalar(select(Source).where(Source.link == link))
            if row is None:
                row = Source(
                    link=link,
                    title=title,
                    telegram_chat_id=telegram_chat_id,
                    join_state=join_state,
                    enabled=True,
                )
                session.add(row)
                await session.flush()
            else:
                row.enabled = True
                row.title = title or row.title
                row.telegram_chat_id = telegram_chat_id or row.telegram_chat_id
                row.join_state = join_state
            return row

    async def list_sources(self, *, enabled_only: bool = False) -> list[Source]:
        async with self.database.session() as session:
            query = select(Source).order_by(Source.id)
            if enabled_only:
                query = query.where(Source.enabled.is_(True))
            return list(await session.scalars(query))

    async def get_source(self, source_id: int) -> Source | None:
        async with self.database.session() as session:
            return await session.get(Source, source_id)

    async def disable_source(self, source_id: int) -> bool:
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                update(Source).where(Source.id == source_id).values(enabled=False)
            )
            return bool(result.rowcount)

    @staticmethod
    def _source_post_key(candidate: CandidateInput) -> str:
        if candidate.album_group_id:
            return f"album:{candidate.album_group_id}"
        return "messages:" + ",".join(str(value) for value in candidate.message_ids)

    async def _find_duplicate(self, session, candidate: CandidateInput):
        source_key = self._source_post_key(candidate)
        existing = await session.scalar(
            select(Candidate).where(
                Candidate.source_id == candidate.source_id,
                Candidate.source_post_key == source_key,
            )
        )
        if existing:
            return existing, "source_post"
        media_identity = telegram_media_identity(candidate.media)
        if media_identity:
            existing = await session.scalar(
                select(Candidate).where(Candidate.telegram_media_identity == media_identity)
            )
            if existing:
                return existing, "telegram_media"
        existing = await session.scalar(
            select(Candidate).where(
                Candidate.metadata_fingerprint == candidate.metadata_fingerprint
            )
        )
        if existing:
            return existing, "metadata"
        return None, None

    async def upsert_candidate(self, value: CandidateInput) -> CandidateInsertResult:
        async with self.database.session() as session, session.begin():
            existing, reason = await self._find_duplicate(session, value)
            if existing:
                return CandidateInsertResult(existing.id, False, reason)

            media_kind = value.media[0].kind.value if value.media else "none"
            row = Candidate(
                source_id=value.source_id,
                telegram_chat_id=value.telegram_chat_id,
                source_post_key=self._source_post_key(value),
                message_ids=list(value.message_ids),
                album_group_id=value.album_group_id,
                published_at=value.published_at,
                cleaned_html=value.cleaned_html,
                media_kind=media_kind,
                media_json=[{**asdict(item), "kind": item.kind.value} for item in value.media],
                telegram_media_identity=telegram_media_identity(value.media),
                metadata_fingerprint=value.metadata_fingerprint,
                views=value.views,
                forwards=value.forwards,
                reactions=value.reactions,
            )
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                found, duplicate_reason = await self._find_duplicate(session, value)
                if found:
                    return CandidateInsertResult(found.id, False, duplicate_reason)
                raise
            return CandidateInsertResult(row.id, True)

    async def enqueue(
        self, candidate_id: int, scheduled_at: datetime, content_type: ContentType
    ) -> bool:
        async with self.database.session() as session, session.begin():
            row = QueueItem(
                candidate_id=candidate_id,
                scheduled_at=scheduled_at,
                slot_rule=content_type.value,
                state="pending",
            )
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                return False
            return True

    async def list_unqueued_candidates(self) -> list[Candidate]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(Candidate)
                .join(Source, Source.id == Candidate.source_id)
                .outerjoin(QueueItem, QueueItem.candidate_id == Candidate.id)
                .where(
                    Source.enabled.is_(True),
                    Candidate.state == "eligible",
                    QueueItem.id.is_(None),
                )
                .order_by(Candidate.published_at.desc(), Candidate.id.desc())
            )
            return list(result)

    async def list_queue(self) -> list[QueueView]:
        async with self.database.session() as session:
            rows = await session.execute(
                select(
                    QueueItem.id,
                    QueueItem.candidate_id,
                    QueueItem.scheduled_at,
                    QueueItem.slot_rule,
                    QueueItem.state,
                    Candidate.source_id,
                    Source.title,
                    Candidate.source_post_key,
                    Candidate.media_kind,
                )
                .join(Candidate, Candidate.id == QueueItem.candidate_id)
                .join(Source, Source.id == Candidate.source_id)
                .order_by(QueueItem.scheduled_at, QueueItem.id)
            )
            return [QueueView(*row) for row in rows]

    async def delete_unpublished_queue(self) -> int:
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                delete(QueueItem).where(
                    QueueItem.state.in_({"pending", "failed", "skipped_duplicate"})
                )
            )
            return result.rowcount or 0

    async def claim_due_item(self, now: datetime, *, lease_seconds: int = 300) -> QueueItem | None:
        token = uuid.uuid4().hex
        eligible = or_(
            QueueItem.state == "pending",
            and_(QueueItem.state == "processing", QueueItem.lease_until < now),
        )
        candidate_id = (
            select(QueueItem.id)
            .where(QueueItem.scheduled_at <= now, eligible)
            .order_by(QueueItem.scheduled_at, QueueItem.id)
            .limit(1)
            .scalar_subquery()
        )
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                update(QueueItem)
                .where(QueueItem.id == candidate_id, eligible)
                .values(
                    state="processing",
                    lease_token=token,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    attempts=QueueItem.attempts + 1,
                )
                .returning(QueueItem)
            )
            return result.scalar_one_or_none()

    async def complete_item(
        self, queue_item_id: int, lease_token: str, max_message_ids: list[str]
    ) -> bool:
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                update(QueueItem)
                .where(
                    QueueItem.id == queue_item_id,
                    QueueItem.state == "processing",
                    QueueItem.lease_token == lease_token,
                )
                .values(
                    state="published",
                    lease_token=None,
                    lease_until=None,
                    max_message_ids=max_message_ids,
                    published_at=datetime.now(UTC),
                )
            )
            return bool(result.rowcount)

    async def fail_item(
        self, queue_item_id: int, lease_token: str, error: str, *, state: str = "failed"
    ) -> bool:
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                update(QueueItem)
                .where(
                    QueueItem.id == queue_item_id,
                    QueueItem.state == "processing",
                    QueueItem.lease_token == lease_token,
                )
                .values(
                    state=state,
                    lease_token=None,
                    lease_until=None,
                    last_error=error[:2000],
                )
            )
            return bool(result.rowcount)

    async def get_queue_item(self, queue_item_id: int) -> QueueItem | None:
        async with self.database.session() as session:
            return await session.get(QueueItem, queue_item_id)

    async def record_media_hash(self, sha256: str, candidate_id: int) -> bool:
        async with self.database.session() as session, session.begin():
            try:
                async with session.begin_nested():
                    session.add(MediaHash(sha256=sha256, candidate_id=candidate_id))
                    await session.flush()
            except IntegrityError:
                return False
            return True
