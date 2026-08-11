const TELEGRAM_API = "https://api.telegram.org";
const TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024;

export class RemoteApiError extends Error {
  constructor(service, message, status, code) {
    super(`${service}: ${message}`);
    this.name = "RemoteApiError";
    this.service = service;
    this.status = status;
    this.code = code;
  }
}

export class TelegramFileTooLargeError extends Error {
  constructor(size) {
    super(`Telegram-файл больше 20 МБ (${Math.ceil(size / 1024 / 1024)} МБ)`);
    this.name = "TelegramFileTooLargeError";
    this.code = "TELEGRAM_FILE_TOO_LARGE";
  }
}

async function telegramCall(token, method, body, fetchImpl = fetch) {
  const response = await fetchImpl(`${TELEGRAM_API}/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) {
    throw new RemoteApiError(
      "Telegram",
      data.description || `HTTP ${response.status}`,
      response.status,
      data.error_code,
    );
  }
  return data.result;
}

export async function downloadTelegramFile(token, file, fetchImpl = fetch) {
  let info;
  try {
    info = await telegramCall(
      token,
      "getFile",
      { file_id: file.fileId },
      fetchImpl,
    );
  } catch (error) {
    if (
      error instanceof RemoteApiError &&
      error.code === 400 &&
      /file is too big/i.test(error.message)
    ) {
      throw new TelegramFileTooLargeError(TELEGRAM_DOWNLOAD_LIMIT + 1);
    }
    throw error;
  }
  if (info.file_size > TELEGRAM_DOWNLOAD_LIMIT) {
    throw new TelegramFileTooLargeError(info.file_size);
  }

  const response = await fetchImpl(
    `${TELEGRAM_API}/file/bot${token}/${info.file_path}`,
  );
  if (!response.ok) {
    throw new RemoteApiError(
      "Telegram",
      `не удалось скачать файл, HTTP ${response.status}`,
      response.status,
    );
  }

  const length = Number(response.headers.get("content-length") || 0);
  if (length > TELEGRAM_DOWNLOAD_LIMIT) {
    throw new TelegramFileTooLargeError(length);
  }

  const blob = await response.blob();
  if (blob.size > TELEGRAM_DOWNLOAD_LIMIT) {
    throw new TelegramFileTooLargeError(blob.size);
  }

  const pathName = info.file_path.split("/").pop() || file.fileName;
  return {
    blob,
    fileName: sanitizeFileName(file.fileName || pathName || "telegram-file"),
  };
}

function sanitizeFileName(value) {
  return value.replace(/[^\p{L}\p{N}._ -]/gu, "_").slice(0, 180);
}

function largestPhoto(photo = []) {
  return [...photo].sort(
    (a, b) => (b.file_size ?? b.width * b.height) - (a.file_size ?? a.width * a.height),
  )[0];
}

export function extractTelegramMedia(message) {
  const files = [];
  const photo = largestPhoto(message.photo);
  if (photo) {
    files.push({
      type: "image",
      fileId: photo.file_id,
      fileName: `photo-${message.message_id}.jpg`,
    });
  }

  const candidates = [
    ["video", message.video, `video-${message.message_id}.mp4`],
    ["video", message.animation, `animation-${message.message_id}.mp4`],
    ["video", message.video_note, `video-note-${message.message_id}.mp4`],
    ["audio", message.audio, message.audio?.file_name || `audio-${message.message_id}.mp3`],
    ["audio", message.voice, `voice-${message.message_id}.ogg`],
    ["file", message.document, message.document?.file_name || `file-${message.message_id}`],
    ["file", message.sticker, `sticker-${message.message_id}.${message.sticker?.is_animated ? "tgs" : message.sticker?.is_video ? "webm" : "webp"}`],
  ];

  for (const [type, value, fileName] of candidates) {
    if (value?.file_id) {
      files.push({ type, fileId: value.file_id, fileName });
    }
  }
  return files;
}

export function messageText(message) {
  if (message.text) {
    return { text: message.text, entities: message.entities ?? [] };
  }
  if (message.caption) {
    return {
      text: message.caption,
      entities: message.caption_entities ?? [],
    };
  }
  if (message.poll) {
    const options = message.poll.options
      .map((option) => `• ${option.text}`)
      .join("\n");
    return {
      text: `${message.poll.question}\n\n${options}`,
      entities: [],
    };
  }
  if (message.location) {
    const { latitude, longitude } = message.location;
    return {
      text: `Геолокация: https://maps.google.com/?q=${latitude},${longitude}`,
      entities: [],
    };
  }
  if (message.contact) {
    const name = [message.contact.first_name, message.contact.last_name]
      .filter(Boolean)
      .join(" ");
    return {
      text: `Контакт: ${name}${message.contact.phone_number ? `, ${message.contact.phone_number}` : ""}`,
      entities: [],
    };
  }
  if (message.dice) {
    return { text: `🎲 Выпало: ${message.dice.emoji} ${message.dice.value}`, entities: [] };
  }
  return { text: "", entities: [] };
}
