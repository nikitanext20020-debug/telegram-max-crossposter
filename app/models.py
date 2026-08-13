from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SettingsRow(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_text: Mapped[str] = mapped_column(String(500), nullable=False)
    signature_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    max_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    queue_target: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_low_water: Mapped[int] = mapped_column(Integer, nullable=False)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link: Mapped[str] = mapped_column(String(2000), unique=True, nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    join_state: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    cursor_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    candidates: Mapped[list[Candidate]] = relationship(back_populates="source")


class ScheduleSlot(Base):
    __tablename__ = "schedule_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_time: Mapped[str] = mapped_column(String(5), unique=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    qstash_schedule_id: Mapped[str | None] = mapped_column(String(200))


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("source_id", "source_post_key", name="uq_candidate_source_post"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_post_key: Mapped[str] = mapped_column(String(200), nullable=False)
    message_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    album_group_id: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleaned_html: Mapped[str] = mapped_column(Text, nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    media_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    telegram_media_identity: Mapped[str | None] = mapped_column(String(64), unique=True)
    metadata_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    forwards: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reactions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="eligible")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    source: Mapped[Source] = relationship(back_populates="candidates")
    queue_item: Mapped[QueueItem | None] = relationship(back_populates="candidate")


class QueueItem(Base):
    __tablename__ = "queue_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id"), unique=True, nullable=False
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), unique=True, nullable=False
    )
    slot_rule: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    max_message_ids: Mapped[list[str] | None] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidate: Mapped[Candidate] = relationship(back_populates="queue_item")


class MediaHash(Base):
    __tablename__ = "media_hashes"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    max_message_ids: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queue_item_id: Mapped[int | None] = mapped_column(ForeignKey("queue_items.id"))
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class JobLock(Base):
    __tablename__ = "job_locks"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
