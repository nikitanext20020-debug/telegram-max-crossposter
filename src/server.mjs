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
  onReady: publishMessage,
});

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
  return new Promise((resolve, reject) => {
    let batch = albumBatches.get(key);
    if (!batch) {
      batch = { messages: [], waiters: [], timer: null };
      albumBatches.set(key, batch);
    }
    batch.messages.push(message);
    batch.waiters.push({ resolve, reject });
    clearTimeout(batch.timer);
    batch.timer = setTimeout(async () => {
      albumBatches.delete(key);
      try {
        const result = await crosspostMessages(config, batch.messages);
        for (const waiter of batch.waiters) waiter.resolve(result);
      } catch (error) {
        for (const waiter of batch.waiters) waiter.reject(error);
      }
    }, 1500);
  });
}

function publishMessage(message) {
  if (message.media_group_id) return enqueueAlbum(message);
  return crosspostMessages(config, [message]);
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
        result: result?.ignored || result?.duplicate ? result : "published",
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
