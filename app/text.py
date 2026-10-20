from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class TextEntity:
    type: str
    offset: int
    length: int
    url: str | None = None


@dataclass(frozen=True, slots=True)
class CleanText:
    text: str
    styles: tuple[tuple[str, ...], ...]


FORMAT_TAGS = {
    "blockquote": ("<blockquote>", "</blockquote>"),
    "expandable_blockquote": ("<blockquote>", "</blockquote>"),
    "pre": ("<pre>", "</pre>"),
    "code": ("<code>", "</code>"),
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
}
FORMAT_ORDER = tuple(FORMAT_TAGS)
ALLOWED_SOURCE_LINK_URL = "https://t.me/NosokVPNBot?start=partner_8235497168"
ALLOWED_LINK_STYLE = "allowed_source_link"
RENDER_TAGS = {
    **FORMAT_TAGS,
    ALLOWED_LINK_STYLE: (
        f'<a href="{html.escape(ALLOWED_SOURCE_LINK_URL, quote=True)}">',
        "</a>",
    ),
}
VISIBLE_LINK_TYPES = {"url", "email", "mention"}
HIDDEN_LINK_TYPES = {"text_link", "text_mention"}
URL_PATTERN = re.compile(
    r"(?i)(?<![\w@])(?:https?://[^\s<>()]+|(?:t\.me|max\.ru|telegram\.me|telegram\.dog)/[^\s<>()]+)"
)
EMAIL_PATTERN = re.compile(r"(?i)(?<![\w@])[\w.+-]+@[\w.-]+\.[a-z]{2,}")
MENTION_PATTERN = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,}")
PROMO_PATTERN = re.compile(
    r"(?i)\b(?:подпис\w*|контент\w*|канал\w*|фулл\w*|бот\w*|реклам\w*|"
    r"ссылк\w*|telegram|телеграм|мы\s+в)\b"
)
TRAILING_URL_PUNCTUATION = ".,!?;:)]}»”'"


def _utf16_to_python(text: str, offset: int) -> int | None:
    if offset < 0:
        return None
    units = 0
    for index, character in enumerate(text):
        if units == offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > offset:
            return None
    return len(text) if units == offset else None


def _entity_range(text: str, entity: TextEntity) -> tuple[int, int] | None:
    start = _utf16_to_python(text, entity.offset)
    end = _utf16_to_python(text, entity.offset + entity.length)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _raw_link_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for pattern in (URL_PATTERN, EMAIL_PATTERN, MENTION_PATTERN):
        for match in pattern.finditer(text):
            end = match.end()
            if pattern is URL_PATTERN:
                while end > match.start() and text[end - 1] in TRAILING_URL_PUNCTUATION:
                    end -= 1
            if end > match.start():
                ranges.append((match.start(), end))
    return ranges


def _line_ranges(text: str) -> list[tuple[int, int]]:
    result = []
    start = 0
    for line in text.splitlines(keepends=True):
        end = start + len(line)
        result.append((start, end))
        start = end
    if not text or start < len(text) or text.endswith(("\n", "\r")):
        result.append((start, len(text)))
    return result


def _meaningful_positions(text: str, start: int, end: int) -> set[int]:
    return {index for index in range(start, end) if text[index].isalnum()}


def _promotional_lines(
    text: str,
    raw_links: list[tuple[int, int]],
    hidden_links: list[tuple[int, int]],
) -> set[int]:
    removed: set[int] = set()
    all_links = raw_links + hidden_links
    for start, end in _line_ranges(text):
        links = [item for item in all_links if item[0] < end and item[1] > start]
        if not links:
            continue
        line_text = text[start:end]
        linked_positions = {
            index
            for link_start, link_end in links
            for index in range(max(start, link_start), min(end, link_end))
        }
        meaningful = _meaningful_positions(text, start, end)
        all_words_are_linked = bool(meaningful) and meaningful <= linked_positions
        has_raw_link = any(item in raw_links for item in links)
        if all_words_are_linked or (has_raw_link and PROMO_PATTERN.search(line_text)):
            removed.update(range(start, end))
    return removed


def _normalize_characters(
    characters: list[str], styles: list[tuple[str, ...]]
) -> tuple[str, tuple[tuple[str, ...], ...]]:
    output_chars: list[str] = []
    output_styles: list[tuple[str, ...]] = []
    pending_space = False
    pending_style: tuple[str, ...] = ()
    newline_count = 0

    for character, style in zip(characters, styles, strict=True):
        if character in "\r\n":
            while output_chars and output_chars[-1] == " ":
                output_chars.pop()
                output_styles.pop()
            pending_space = False
            newline_count += 1
            if output_chars and newline_count <= 2:
                output_chars.append("\n")
                output_styles.append(())
            continue
        newline_count = 0
        if character.isspace():
            if output_chars and output_chars[-1] != "\n":
                pending_space = True
                pending_style = style
            continue
        if pending_space:
            output_chars.append(" ")
            output_styles.append(pending_style)
            pending_space = False
        output_chars.append(character)
        output_styles.append(style)

    while output_chars and output_chars[-1] in {" ", "\n"}:
        output_chars.pop()
        output_styles.pop()
    return "".join(output_chars), tuple(output_styles)


def clean_telegram_text(
    text: str = "",
    entities: list[TextEntity] | tuple[TextEntity, ...] = (),
    *,
    button_urls: list[str] | tuple[str, ...] = (),
) -> CleanText:
    del button_urls  # Buttons are deliberately never copied to MAX.
    valid: list[tuple[TextEntity, int, int]] = []
    for entity in entities:
        entity_range = _entity_range(text, entity)
        if entity_range is not None:
            valid.append((entity, *entity_range))

    raw_links = _raw_link_ranges(text)
    visible_links = [
        (start, end) for entity, start, end in valid if entity.type in VISIBLE_LINK_TYPES
    ]
    # Match the full target, not the label, bot name, domain, or URL prefix.
    allowed_links = [
        (start, end)
        for entity, start, end in valid
        if entity.type == "text_link" and entity.url == ALLOWED_SOURCE_LINK_URL
    ]
    hidden_links = [
        (start, end)
        for entity, start, end in valid
        if entity.type in HIDDEN_LINK_TYPES
        and not (entity.type == "text_link" and entity.url == ALLOWED_SOURCE_LINK_URL)
    ]
    link_ranges = list({*raw_links, *visible_links})
    removed = {index for start, end in link_ranges for index in range(start, end)}
    removed.update(_promotional_lines(text, raw_links + visible_links, hidden_links))

    formatting = [
        (entity.type, start, end) for entity, start, end in valid if entity.type in FORMAT_TAGS
    ]
    characters: list[str] = []
    styles: list[tuple[str, ...]] = []
    for index, character in enumerate(text):
        if index in removed:
            continue
        active = {style for style, start, end in formatting if start <= index < end}
        link_style = (
            (ALLOWED_LINK_STYLE,)
            if any(start <= index < end for start, end in allowed_links)
            else ()
        )
        characters.append(character)
        styles.append(link_style + tuple(style for style in FORMAT_ORDER if style in active))

    normalized_text, normalized_styles = _normalize_characters(characters, styles)
    return CleanText(normalized_text, normalized_styles)


def render_max_html(clean: CleanText) -> str:
    result: list[str] = []
    active: tuple[str, ...] = ()
    for character, desired in zip(clean.text, clean.styles, strict=True):
        common = 0
        while common < min(len(active), len(desired)) and active[common] == desired[common]:
            common += 1
        for style in reversed(active[common:]):
            result.append(RENDER_TAGS[style][1])
        for style in desired[common:]:
            result.append(RENDER_TAGS[style][0])
        result.append(html.escape(character, quote=True))
        active = desired
    for style in reversed(active):
        result.append(RENDER_TAGS[style][1])
    return "".join(result)


def build_signature_html(text: str, raw_url: str) -> str:
    label = text.strip()
    parts = urlsplit(raw_url.strip())
    if not label:
        raise ValueError("Текст подписи не может быть пустым")
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Ссылка подписи должна быть корректным http(s) URL")
    signature = (
        f'\n\n<a href="{html.escape(raw_url.strip(), quote=True)}">'
        f"{html.escape(label, quote=True)}</a>"
    )
    if len(signature) >= 1000:
        raise ValueError("Подпись слишком длинная")
    return signature


TAG_PATTERN = re.compile(r"(<[^>]+>|&(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)")


def _html_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0
    for match in TAG_PATTERN.finditer(value):
        tokens.extend(value[cursor : match.start()])
        tokens.append(match.group())
        cursor = match.end()
    tokens.extend(value[cursor:])
    return tokens


def _tag_name(token: str) -> str | None:
    match = re.fullmatch(r"</?([a-z]+)(?:\s[^>]*)?>", token, re.IGNORECASE)
    return match.group(1).lower() if match else None


def append_signature(html_text: str, signature: str, limit: int = 4000) -> list[str]:
    if len(signature.lstrip()) > limit:
        raise ValueError("Подпись превышает лимит MAX")
    if not html_text:
        return [signature.lstrip()]
    if len(html_text) + len(signature) <= limit:
        return [html_text + signature]

    chunks: list[str] = []
    current = ""
    active: list[tuple[str, str]] = []

    def closers() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(active))

    def flush() -> None:
        nonlocal current
        if not current:
            return
        chunks.append(current + closers() + signature)
        current = "".join(opening for _, opening in active)

    for token in _html_tokens(html_text):
        name = _tag_name(token)
        is_close = token.startswith("</")
        if name and not is_close:
            projected_close = f"</{name}>" + closers()
            if (
                current
                and len(current) + len(token) + len(projected_close) + len(signature) > limit
            ):
                flush()
            current += token
            active.append((name, token))
            continue
        if name and is_close:
            if active and active[-1][0] == name:
                active.pop()
            current += token
            continue
        if len(current) + len(token) + len(closers()) + len(signature) > limit:
            flush()
        current += token
    flush()
    return chunks
