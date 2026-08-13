from __future__ import annotations

import asyncio
from datetime import time
from urllib.parse import urlencode

from qstash import QStash, Receiver

from app.config import ScheduleRule


def cron_for_slot(slot: time) -> str:
    return f"CRON_TZ=Europe/Moscow {slot.minute} {slot.hour} * * *"


class QStashService:
    MANAGED_PREFIX = "tgmax-"

    def __init__(
        self,
        *,
        token: str,
        current_signing_key: str,
        next_signing_key: str,
        public_url: str,
        receiver=None,
        client=None,
    ):
        self.public_url = public_url.rstrip("/")
        self.receiver = receiver or Receiver(
            current_signing_key=current_signing_key,
            next_signing_key=next_signing_key,
        )
        self.client = client or QStash(token)

    def verify(self, *, body: bytes, signature: str, path_and_query: str) -> None:
        if not signature:
            raise ValueError("Отсутствует подпись QStash")
        self.receiver.verify(
            signature=signature,
            body=body.decode("utf-8"),
            url=f"{self.public_url}{path_and_query}",
        )

    async def reconcile(self, rules: list[ScheduleRule]) -> set[str]:
        desired = {"tgmax-maintain"}
        for rule in rules:
            desired.add(f"tgmax-publish-{rule.time.strftime('%H%M')}")

        existing = await asyncio.to_thread(self.client.schedule.list)
        for schedule in existing:
            schedule_id = schedule.schedule_id
            if schedule_id.startswith(self.MANAGED_PREFIX) and schedule_id not in desired:
                await asyncio.to_thread(self.client.schedule.delete, schedule_id)

        await asyncio.to_thread(
            self.client.schedule.create,
            destination=f"{self.public_url}/jobs/maintain",
            cron="CRON_TZ=Europe/Moscow */30 * * * *",
            body="{}",
            content_type="application/json",
            method="POST",
            retries=3,
            timeout="5m",
            schedule_id="tgmax-maintain",
        )
        for rule in rules:
            raw_time = rule.time.isoformat(timespec="minutes")
            schedule_id = f"tgmax-publish-{rule.time.strftime('%H%M')}"
            query = urlencode({"slot": raw_time})
            await asyncio.to_thread(
                self.client.schedule.create,
                destination=f"{self.public_url}/jobs/publish?{query}",
                cron=cron_for_slot(rule.time),
                body="{}",
                content_type="application/json",
                method="POST",
                retries=3,
                timeout="5m",
                schedule_id=schedule_id,
            )
        return desired
