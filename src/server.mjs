import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { loadConfig, sourceChatAllowed } from "./config.mjs";
import { crosspostMessages } from "./crosspost.mjs";
import { createPendingQueue } from "./pending.mjs";

const config = loadConfig();
const albumBatches = new Map();
const processedUpdates = new Set();
const inFlightUpdates = new Map();
const settlingQueue = createPendingQueue({
  delayMs: 10_000,
  onReady: (message) => crosspostMessages(config, [message]),
});

// Telegram delivers each item of an album (media group) as a separate update.
// We buffer them and flush once no new item has arrived for this long. The
// window measures the gap BETWEEN items, so it must be comfortably larger than
// Telegram's inter-item delay (usually a few hundred ms).
const ALBUM_DEBOUNCE_MS = Number.parseInt(
  process.env.ALBUM_DEBOUNCE_MS || "50000",
  10,
);

function safeEqual(received, expected) {
  const left = Buffer.from(received || "");
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

async function readJson(request, limit = 1024 * 1024) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > limit) throw new Error("Слишком большой запрос");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function rememberUpdate(updateId) {
  processedUpdates.add(updateId);
  if (processedUpdates.size > 5000) {
    processedUpdates.delete(processedUpdates.values().next().value);
  }
}

function enqueueAlbum(message) {
  const key = `${message.chat.id}:${message.media_group_id}`;
  let batch = albumBatches.get(key);
  if (!batch) {
    batch = {
      messages: new Map(),
      timer: null,
      settlingTimer: null,
    };
    albumBatches.set(key, batch);
  }

  batch.messages.set(message.message_id, message);
  clearTimeout(batch.timer);
  clearTimeout(batch.settlingTimer);
  batch.timer = setTimeout(() => {
    batch.timer = null;
    batch.settlingTimer = setTimeout(() => flushAlbum(key), 10_000);
  }, ALBUM_DEBOUNCE_MS);
}

async function flushAlbum(key) {
  const batch = albumBatches.get(key);
  if (!batch) return;
  albumBatches.delete(key);

  const messages = [...batch.messages.values()];
  try {
    await crosspostMessages(config, messages);
    console.log(
      JSON.stringify({
        level: "info",
        event: "album_crossposted",
        media_group_id: messages[0]?.media_group_id,
        items: messages.length,
      }),
    );
  } catch (error) {
    console.error(
      JSON.stringify({
        level: "error",
        event: "album_crosspost_failed",
        media_group_id: messages[0]?.media_group_id,
        message: error?.message || "Неизвестная ошибка",
        service: error?.service,
        status: error?.status,
        code: error?.code,
      }),
    );
  }
}

async function processUpdate(update) {
  const message = update.channel_post || update.edited_channel_post;
  if (!message) return { ignored: "not_channel_post" };
  if (!sourceChatAllowed(config, message.chat.id)) {
    return { ignored: "source_chat_not_allowed" };
  }

  console.log(
    JSON.stringify({
      level: "info",
      event: "telegram_post_received",
      update_id: update.update_id,
      chat_id: message.chat.id,
      message_id: message.message_id,
      media_group_id: message.media_group_id ?? null,
      edited: Boolean(update.edited_channel_post),
    }),
  );

  if (message.media_group_id) {
    // Acknowledge this album item immediately instead of awaiting the flush.
    // Telegram delivers album items in order and holds the next one until the
    // current is acked; if we blocked the response on the debounce+crosspost,
    // each item would flush alone and become a separate MAX post. Enqueuing
    // and returning fast lets every item join the same batch, which is then
    // crossposted as a single MAX message in the background.
    enqueueAlbum(message);
    return { queued: "album", media_group_id: message.media_group_id };
  }
  return settlingQueue.enqueue(message);
}

async function processOnce(update) {
  const updateId = update.update_id;
  if (!Number.isInteger(updateId)) throw new Error("Нет update_id");
  if (processedUpdates.has(updateId)) return { duplicate: true };
  if (inFlightUpdates.has(updateId)) return inFlightUpdates.get(updateId);

  const operation = processUpdate(update)
    .then((result) => {
      rememberUpdate(updateId);
      return result;
    })
    .finally(() => inFlightUpdates.delete(updateId));
  inFlightUpdates.set(updateId, operation);
  return operation;
}

function json(response, status, body) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, "http://localhost");

  if (request.method === "GET" && url.pathname === "/healthz") {
    return json(response, 200, { ok: true });
  }

  if (request.method !== "POST" || url.pathname !== "/telegram") {
    return json(response, 404, { ok: false });
  }

  const secret = request.headers["x-telegram-bot-api-secret-token"];
  if (!safeEqual(Array.isArray(secret) ? secret[0] : secret, config.webhookSecret)) {
    return json(response, 401, { ok: false });
  }

  try {
    const update = await readJson(request);
    const result = await processOnce(update);
    console.log(
      JSON.stringify({
        level: "info",
        event: "telegram_post_processed",
        update_id: update.update_id,
        result:
          result?.ignored || result?.duplicate || result?.queued
            ? result
            : "published",
      }),
    );
    return json(response, 200, { ok: true });
  } catch (error) {
    console.error(
      JSON.stringify({
        level: "error",
        message: error?.message || "Неизвестная ошибка",
        service: error?.service,
        status: error?.status,
        code: error?.code,
      }),
    );
    return json(response, 500, { ok: false });
  }
});

server.listen(config.port, "0.0.0.0", () => {
  console.log(`Crossposter is listening on port ${config.port}`);
});
