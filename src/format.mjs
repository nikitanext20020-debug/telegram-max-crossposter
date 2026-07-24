const ENTITY_TAGS = {
  bold: ["<b>", "</b>"],
  italic: ["<i>", "</i>"],
  underline: ["<u>", "</u>"],
  strikethrough: ["<s>", "</s>"],
  code: ["<code>", "</code>"],
  pre: ["<pre>", "</pre>"],
};

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function tagsForEntity(entity, text) {
  if (ENTITY_TAGS[entity.type]) return ENTITY_TAGS[entity.type];
  if (entity.type === "blockquote" || entity.type === "expandable_blockquote") {
    return ["", ""];
  }

  if (entity.type === "text_link") {
    const url = safeHttpUrl(entity.url);
    return url
      ? [`<a href="${escapeHtml(url)}">`, "</a>"]
      : ["", ""];
  }

  if (entity.type === "url") {
    const url = safeHttpUrl(text.slice(entity.offset, entity.offset + entity.length));
    return url
      ? [`<a href="${escapeHtml(url)}">`, "</a>"]
      : ["", ""];
  }

  return ["", ""];
}

export function telegramTextToMaxHtml(text = "", entities = []) {
  if (!text) return "";

  const normalized = entities
    .filter(
      (entity) =>
        Number.isInteger(entity.offset) &&
        Number.isInteger(entity.length) &&
        entity.length > 0 &&
        entity.offset >= 0 &&
        entity.offset + entity.length <= text.length,
    )
    .map((entity) => {
      const [open, close] = tagsForEntity(entity, text);
      return {
        ...entity,
        end: entity.offset + entity.length,
        open,
        close,
      };
    })
    .filter((entity) => entity.open || entity.close);

  const openings = new Map();
  const closings = new Map();
  for (const entity of normalized) {
    const openAt = openings.get(entity.offset) ?? [];
    openAt.push(entity);
    openings.set(entity.offset, openAt);

    const closeAt = closings.get(entity.end) ?? [];
    closeAt.push(entity);
    closings.set(entity.end, closeAt);
  }

  for (const list of openings.values()) {
    list.sort((a, b) => b.end - a.end);
  }
  for (const list of closings.values()) {
    list.sort((a, b) => b.offset - a.offset);
  }

  let result = "";
  for (let index = 0; index <= text.length; ) {
    for (const entity of closings.get(index) ?? []) result += entity.close;
    for (const entity of openings.get(index) ?? []) result += entity.open;
    if (index === text.length) break;

    const codePoint = text.codePointAt(index);
    const character = String.fromCodePoint(codePoint);
    result += escapeHtml(character);
    index += character.length;
  }

  return result;
}

function adjustedEntities(entities, start, end) {
  return entities
    .filter(
      (entity) =>
        entity.offset >= start && entity.offset + entity.length <= end,
    )
    .map((entity) => ({ ...entity, offset: entity.offset - start }));
}

export function splitTelegramText(text = "", entities = [], maxLength = 2800) {
  if (!text) return [{ text: "", entities: [] }];

  const chunks = [];
  let start = 0;
  while (start < text.length) {
    let end = Math.min(start + maxLength, text.length);
    if (end < text.length) {
      if (
        end > start &&
        /[\uD800-\uDBFF]/.test(text[end - 1]) &&
        /[\uDC00-\uDFFF]/.test(text[end])
      ) {
        end -= 1;
      }

      const candidate = text.slice(start, end);
      const newline = candidate.lastIndexOf("\n");
      const space = candidate.lastIndexOf(" ");
      const naturalBreak = Math.max(newline, space);
      if (naturalBreak > Math.floor(maxLength * 0.65)) {
        end = start + naturalBreak + 1;
      }
    }

    chunks.push({
      text: text.slice(start, end),
      entities: adjustedEntities(entities, start, end),
    });
    start = end;
  }
  return chunks;
}

export function buildSignatureHtml(text, rawUrl) {
  if (!text && !rawUrl) return "";
  if (!rawUrl) return `\n\n${escapeHtml(text)}`;

  const url = safeHttpUrl(rawUrl);
  if (!url) {
    throw new Error("MAX_SIGNATURE_URL должен начинаться с http:// или https://");
  }

  const label = text || rawUrl;
  const signature = `\n\n<a href="${escapeHtml(url)}">${escapeHtml(label)}</a>`;
  if (signature.length > 900) {
    throw new Error("Подпись слишком длинная: сократите текст или ссылку");
  }
  return signature;
}
