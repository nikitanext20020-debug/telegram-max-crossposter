from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import ContentType, ScheduleRule
from app.store import Store


@dataclass(frozen=True, slots=True)
class FutureSlot:
    when: datetime
    rule: ContentType


@dataclass(frozen=True, slots=True)
class CandidateChoice:
    id: int
    source_id: int
    media_kind: str
    published_at: datetime
    views: int
    forwards: int
    reactions: int


@dataclass(frozen=True, slots=True)
class RefillResult:
    added: int
    unfilled: int


def next_slots(
    now: datetime,
    rules: list[ScheduleRule] | tuple[ScheduleRule, ...],
    count: int,
    timezone: str,
) -> list[FutureSlot]:
    if now.tzinfo is None:
        raise ValueError("now должен содержать часовой пояс")
    if count <= 0:
        return []
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    day: date = local_now.date()
    result: list[FutureSlot] = []
    sorted_rules = sorted(rules, key=lambda rule: rule.time)
    if not sorted_rules:
        return []
    while len(result) < count:
        for rule in sorted_rules:
            local_value = datetime.combine(day, rule.time, tzinfo=zone)
            utc_value = local_value.astimezone(UTC)
            if utc_value > now.astimezone(UTC):
                result.append(FutureSlot(utc_value, rule.content_type))
                if len(result) == count:
                    break
        day += timedelta(days=1)
    return result


def engagement_score(candidate: CandidateChoice, *, top_mode: bool = False) -> float:
    weighted = (
        max(0, candidate.views) + max(0, candidate.forwards) * 5 + max(0, candidate.reactions) * 3
    )
    return float(weighted) if top_mode else math.log1p(weighted)


def choose_candidate(
    slot_rule: ContentType,
    candidates: list[CandidateChoice],
    recent_source_ids: list[int],
    *,
    top_mode: bool = False,
) -> CandidateChoice | None:
    eligible = [
        candidate
        for candidate in candidates
        if slot_rule is ContentType.ANY or candidate.media_kind == slot_rule.value
    ]
    if not eligible:
        return None
    if recent_source_ids:
        rotated = [
            candidate for candidate in eligible if candidate.source_id != recent_source_ids[-1]
        ]
        if rotated:
            eligible = rotated

    def ranking(candidate: CandidateChoice):
        freshness = candidate.published_at.timestamp()
        activity = engagement_score(candidate, top_mode=top_mode)
        return (
            (activity, freshness, candidate.id)
            if top_mode
            else (
                freshness,
                activity,
                candidate.id,
            )
        )

    return max(eligible, key=ranking)


class QueuePlanner:
    def __init__(self, store: Store):
        self.store = store

    async def refill(
        self, now: datetime, *, target: int | None = None, top_mode: bool = False
    ) -> RefillResult:
        settings = await self.store.get_settings()
        schedule_rows = await self.store.list_schedule()
        rules = [
            ScheduleRule(
                datetime.strptime(row.local_time, "%H:%M").time(),
                ContentType(row.content_type),
            )
            for row in schedule_rows
        ]
        queue = await self.store.list_queue()
        pending = [row for row in queue if row.state in {"pending", "processing"}]
        desired = settings.queue_target if target is None else target
        needed = max(0, desired - len(pending))
        if needed == 0:
            return RefillResult(0, 0)

        occupied = {
            row.scheduled_at.replace(tzinfo=UTC)
            if row.scheduled_at.tzinfo is None
            else row.scheduled_at.astimezone(UTC)
            for row in queue
        }
        generated = next_slots(
            now,
            rules,
            needed + len(occupied) + max(10, needed),
            settings.timezone,
        )
        slots = [slot for slot in generated if slot.when not in occupied][:needed]
        model_candidates = await self.store.list_unqueued_candidates()
        candidates = [
            CandidateChoice(
                id=row.id,
                source_id=row.source_id,
                media_kind=row.media_kind,
                published_at=row.published_at.replace(tzinfo=UTC)
                if row.published_at.tzinfo is None
                else row.published_at,
                views=row.views,
                forwards=row.forwards,
                reactions=row.reactions,
            )
            for row in model_candidates
        ]
        recent_sources = [row.source_id for row in pending]
        added = 0
        unfilled = 0
        for slot in slots:
            selected = choose_candidate(
                slot.rule,
                candidates,
                recent_sources,
                top_mode=top_mode,
            )
            if selected is None:
                unfilled += 1
                continue
            if await self.store.enqueue(selected.id, slot.when, slot.rule):
                added += 1
                recent_sources.append(selected.source_id)
                candidates = [item for item in candidates if item.id != selected.id]
            else:
                unfilled += 1
        return RefillResult(added, unfilled)

    async def refresh(
        self, now: datetime, *, target: int | None = None, top_mode: bool = False
    ) -> RefillResult:
        await self.store.delete_unpublished_queue()
        return await self.refill(now, target=target, top_mode=top_mode)
