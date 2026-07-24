import {
  buildSignatureHtml,
  escapeHtml,
  splitTelegramText,
  telegramTextToMaxHtml,
} from "./format.mjs";
import {
  downloadTelegramFile,
  extractTelegramMedia,
  messageText,
  TelegramFileTooLargeError,
} from "./telegram.mjs";
import { sendMaxMessage, uploadToMax } from "./max.mjs";

async function mapLimit(items, limit, mapper) {
  const results = new Array(items.length);
  let cursor = 0;

  async function worker() {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await mapper(items[index], index);
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, () => worker()),
  );
  return results;
}

function chooseText(messages) {
  for (const message of messages) {
    const content = messageText(message);
    if (content.text) return content;
  }
  return { text: "", entities: [] };
}

function groupAttachments(attachments) {
  const groups = [];
  const visual = attachments.filter(({ type }) =>
    ["image", "video"].includes(type),
  );
  for (let index = 0; index < visual.length; index += 12) {
    groups.push(visual.slice(index, index + 12));
  }

  for (const attachment of attachments) {
    if (!["image", "video"].includes(attachment.type)) {
      groups.push([attachment]);
    }
  }
  return groups;
}

export async function crosspostMessages(
  config,
  messages,
  { externalFetch = fetch, maxFetch } = {},
) {
  const sorted = [...messages].sort((a, b) => a.message_id - b.message_id);
  const content = chooseText(sorted);
  const textChunks = splitTelegramText(content.text, content.entities);
  const signature = buildSignatureHtml(
    config.signatureText,
    config.signatureUrl,
  );

  const media = sorted.flatMap(extractTelegramMedia);
  const tooLarge = [];
  const attachments = await mapLimit(media, 3, async (file) => {
    try {
      const downloaded = await downloadTelegramFile(
        config.telegramToken,
        file,
        externalFetch,
      );
      return uploadToMax(
        config,
        file.type,
        downloaded.blob,
        downloaded.fileName,
        externalFetch,
        maxFetch,
      );
    } catch (error) {
      if (error instanceof TelegramFileTooLargeError) {
        tooLarge.push(file.fileName);
        return null;
      }
      throw error;
    }
  });

  const usableAttachments = attachments.filter(Boolean);
  const attachmentGroups = groupAttachments(usableAttachments);
  const fallback =
    tooLarge.length > 0
      ? `\n\n<i>Вложение больше 20 МБ и не перенесено из Telegram.</i>`
      : "";
  const messageCount = Math.max(textChunks.length, attachmentGroups.length, 1);
  const results = [];

  for (let index = 0; index < messageCount; index += 1) {
    const chunk = textChunks[index];
    let html = chunk
      ? telegramTextToMaxHtml(chunk.text, chunk.entities)
      : "";
    let text = `${html}${fallback}${signature}`;
    if (text.length > 4000 && chunk) {
      // Entity markup can be much longer than the visible Telegram text.
      // MAX limits the serialized text, so retain the content safely.
      html = escapeHtml(chunk.text);
      text = `${html}${fallback}${signature}`;
    }
    results.push(
      await sendMaxMessage(
        config,
        {
          text,
          attachments: attachmentGroups[index] ?? [],
        },
        maxFetch,
      ),
    );
  }
  return results;
}
