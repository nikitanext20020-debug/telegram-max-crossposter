from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MediaKind(StrEnum):
    NONE = "none"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class MediaItem:
    kind: MediaKind
    telegram_file_id: str | None
    telegram_unique_id: str | None
    size: int | None
    duration: int | None
    width: int | None
    height: int | None
    file_name: str


@dataclass(frozen=True, slots=True)
class CandidateInput:
    source_id: int
    telegram_chat_id: int
    message_ids: tuple[int, ...]
    album_group_id: str | None
    published_at: datetime
    cleaned_html: str
    media: tuple[MediaItem, ...]
    views: int = 0
    forwards: int = 0
    reactions: int = 0
    metadata_fingerprint: str = ""


def metadata_fingerprint(cleaned_html: str, media: tuple[MediaItem, ...]) -> str:
    normalized_text = re.sub(r"\s+", " ", cleaned_html).strip().casefold()
    payload = {
        "text": normalized_text,
        "media": [
            {
                "kind": item.kind.value,
                "size": item.size,
                "duration": item.duration,
                "width": item.width,
                "height": item.height,
                "extension": item.file_name.rsplit(".", 1)[-1].casefold()
                if "." in item.file_name
                else "",
            }
            for item in media
        ],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def telegram_media_identity(media: tuple[MediaItem, ...]) -> str | None:
    identities = sorted(item.telegram_unique_id for item in media if item.telegram_unique_id)
    if not identities:
        return None
    return hashlib.sha256("\n".join(identities).encode()).hexdigest()
