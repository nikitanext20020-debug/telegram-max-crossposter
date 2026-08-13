from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.max_client import AmbiguousMaxSendError, validate_media_metadata
from app.store import Store
from app.text import append_signature, build_signature_html


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    state: str
    queue_item_id: int | None = None
    candidate_id: int | None = None
    max_message_ids: tuple[str, ...] = ()
    error: str | None = None


def _sha256_sync(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def sha256_file(path: Path) -> str:
    return await asyncio.to_thread(_sha256_sync, path)


def _attachment_groups(attachments: list[dict]) -> list[list[dict]]:
    visual = [item for item in attachments if item.get("type") in {"image", "video"}]
    groups = [visual[index : index + 12] for index in range(0, len(visual), 12)]
    groups.extend([item] for item in attachments if item.get("type") not in {"image", "video"})
    return groups


class Publisher:
    def __init__(self, store: Store, reader, max_client):
        self.store = store
        self.reader = reader
        self.max_client = max_client

    async def publish_due(self, now: datetime) -> PublishOutcome:
        claim = await self.store.claim_due_item(now)
        if claim is None:
            return PublishOutcome("idle")
        candidate = await self.store.get_candidate(claim.candidate_id)
        if candidate is None:
            await self.store.fail_item(claim.id, claim.lease_token, "Кандидат очереди не найден")
            return PublishOutcome("failed", claim.id, claim.candidate_id)
        if candidate.state == "published":
            await self.store.fail_item(
                claim.id,
                claim.lease_token,
                "Кандидат уже публиковался",
                state="skipped_duplicate",
            )
            return PublishOutcome("skipped_duplicate", claim.id, candidate.id)

        sent_ids: list[str] = []
        try:
            if not await self._deliver(candidate, sent_ids):
                await self.store.fail_item(
                    claim.id,
                    claim.lease_token,
                    "Файл уже публиковался",
                    state="skipped_duplicate",
                )
                return PublishOutcome("skipped_duplicate", claim.id, candidate.id)

            completed = await self.store.complete_item(claim.id, claim.lease_token, sent_ids)
            if not completed:
                raise AmbiguousMaxSendError("MAX опубликован, но результат не сохранён в очереди")
            return PublishOutcome("published", claim.id, candidate.id, tuple(sent_ids))
        except AmbiguousMaxSendError as error:
            await self.store.fail_item(claim.id, claim.lease_token, str(error), state="ambiguous")
            return PublishOutcome(
                "ambiguous",
                claim.id,
                candidate.id,
                tuple(sent_ids),
                str(error),
            )
        except Exception as error:
            state = "ambiguous" if sent_ids else "failed"
            await self.store.fail_item(claim.id, claim.lease_token, str(error), state=state)
            return PublishOutcome(
                state,
                claim.id,
                candidate.id,
                tuple(sent_ids),
                str(error),
            )

    async def publish_now(self, link: str) -> PublishOutcome:
        candidate_id = await self.reader.import_post(link)
        candidate = await self.store.get_candidate(candidate_id)
        if candidate is None:
            return PublishOutcome("failed", candidate_id=candidate_id, error="Пост не найден")
        if candidate.state == "published":
            return PublishOutcome("skipped_duplicate", candidate_id=candidate_id)

        sent_ids: list[str] = []
        try:
            if not await self._deliver(candidate, sent_ids):
                return PublishOutcome("skipped_duplicate", candidate_id=candidate.id)
            if not await self.store.mark_candidate_published(candidate.id):
                raise AmbiguousMaxSendError("MAX опубликован, но кандидат не отмечен в базе")
            return PublishOutcome(
                "published",
                candidate_id=candidate.id,
                max_message_ids=tuple(sent_ids),
            )
        except AmbiguousMaxSendError as error:
            return PublishOutcome(
                "ambiguous",
                candidate_id=candidate.id,
                max_message_ids=tuple(sent_ids),
                error=str(error),
            )
        except Exception as error:
            state = "ambiguous" if sent_ids else "failed"
            return PublishOutcome(
                state,
                candidate_id=candidate.id,
                max_message_ids=tuple(sent_ids),
                error=str(error),
            )

    async def _deliver(self, candidate, sent_ids: list[str]) -> bool:
        settings = await self.store.get_settings()
        signature = build_signature_html(settings.signature_text, settings.signature_url)
        text_chunks = append_signature(candidate.cleaned_html, signature)
        for media in candidate.media_json:
            validate_media_metadata(media)

        with tempfile.TemporaryDirectory(prefix="telegram-max-") as raw_directory:
            paths = await self.reader.download_candidate(candidate, Path(raw_directory))
            if len(paths) != len(candidate.media_json):
                raise RuntimeError("Telegram вернул неполный набор медиа")
            for path, media in zip(paths, candidate.media_json, strict=True):
                self.max_client.validate_media(path, media["kind"])
            hashes = await asyncio.gather(*(sha256_file(path) for path in paths))
            if not await self.store.reserve_media_hashes(list(hashes), candidate.id):
                return False

            attachments = []
            for path, media in zip(paths, candidate.media_json, strict=True):
                attachments.append(await self.max_client.upload(path, media["kind"]))

            groups = _attachment_groups(attachments)
            message_count = max(1, len(text_chunks), len(groups))
            signature_only = append_signature("", signature)[0]
            for index in range(message_count):
                html_text = text_chunks[index] if index < len(text_chunks) else signature_only
                group = groups[index] if index < len(groups) else []
                sent_ids.append(await self.max_client.send(html_text, group))
        return True
