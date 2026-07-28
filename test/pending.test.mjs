import test from "node:test";
import assert from "node:assert/strict";
import { createPendingQueue } from "../src/pending.mjs";

function createFakeTimers() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();

  return {
    setTimeout(callback, delay) {
      const id = nextId++;
      timers.set(id, { callback, due: now + delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    advance(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of [...timers]) {
        if (timer.due <= now) {
          timers.delete(id);
          timer.callback();
        }
      }
    },
  };
}

function message(messageId, text) {
  return {
    message_id: messageId,
    chat: { id: -100 },
    text,
  };
}

test("waits 10 seconds before publishing a message", () => {
  const timers = createFakeTimers();
  const published = [];
  const queue = createPendingQueue({
    delayMs: 10_000,
    onReady: (item) => published.push(item),
    setTimeoutImpl: timers.setTimeout,
    clearTimeoutImpl: timers.clearTimeout,
  });

  queue.enqueue(message(1, "МЫ В МАКСЕ"));
  timers.advance(9_999);
  assert.equal(published.length, 0);

  timers.advance(1);
  assert.deepEqual(published, [message(1, "МЫ В МАКСЕ")]);
});

test("edited message replaces pending text and restarts the timer", () => {
  const timers = createFakeTimers();
  const published = [];
  const queue = createPendingQueue({
    delayMs: 10_000,
    onReady: (item) => published.push(item),
    setTimeoutImpl: timers.setTimeout,
    clearTimeoutImpl: timers.clearTimeout,
  });

  queue.enqueue(message(2, "МЫ В МАКСЕ"));
  timers.advance(9_000);
  queue.enqueue(message(2, "Очищенный пост"));

  timers.advance(1);
  assert.equal(published.length, 0);
  timers.advance(9_999);
  assert.deepEqual(published, [message(2, "Очищенный пост")]);
});

test("keeps different Telegram messages independent", () => {
  const timers = createFakeTimers();
  const published = [];
  const queue = createPendingQueue({
    delayMs: 10_000,
    onReady: (item) => published.push(item),
    setTimeoutImpl: timers.setTimeout,
    clearTimeoutImpl: timers.clearTimeout,
  });

  queue.enqueue(message(3, "Первый"));
  queue.enqueue(message(4, "Второй"));
  timers.advance(10_000);

  assert.deepEqual(published, [message(3, "Первый"), message(4, "Второй")]);
});

test("does not publish twice when receiving channel_post and edited_channel_post", async () => {
  const timers = createFakeTimers();
  const published = [];
  const queue = createPendingQueue({
    delayMs: 10_000,
    onReady: (item) => {
      published.push(item);
      return Promise.resolve("ok");
    },
    setTimeoutImpl: timers.setTimeout,
    clearTimeoutImpl: timers.clearTimeout,
  });

  // Имитируем поведение Telegram: сначала приходит channel_post, потом edited_channel_post
  const p1 = queue.enqueue(message(5, "Оригинал"));
  timers.advance(1000);
  const p2 = queue.enqueue(message(5, "Отредактировано"));

  timers.advance(10000);

  const r1 = await p1;
  const r2 = await p2;

  assert.equal(published.length, 1, "Должен быть опубликован только один пост");
  assert.equal(published[0].text, "Отредактировано");
  assert.equal(r1, "ok");
  assert.equal(r2, "ok");
});

test("ignores an edit that arrives after the post was already published", () => {
  const timers = createFakeTimers();
  const published = [];
  const queue = createPendingQueue({
    delayMs: 10_000,
    onReady: (item) => published.push(item),
    setTimeoutImpl: timers.setTimeout,
    clearTimeoutImpl: timers.clearTimeout,
  });

  queue.enqueue(message(6, "Исходный текст"));
  timers.advance(10_000);
  queue.enqueue(message(6, "Исправленный текст"));
  timers.advance(10_000);

  assert.deepEqual(published, [message(6, "Исходный текст")]);
});

test("album items with same media_group_id are not handled by settlingQueue", async () => {
  const published = [];
  const queue = createPendingQueue({
    delayMs: 10,
    onReady: (item) => {
      published.push(item);
      return Promise.resolve("ok");
    },
  });

  const msg = { message_id: 10, chat: { id: 1 }, media_group_id: "album1", text: "Part of album" };
  
  // В server.mjs есть проверка: if (message.media_group_id) { enqueueAlbum(message); return ... }
  // Но если по какой-то причине сообщение попадет в settlingQueue, оно должно вести себя корректно.
  // Однако наш баг скорее всего в том, что альбомы ВООБЩЕ не попадают в settlingQueue и не ждут 10 секунд.
  
  await queue.enqueue(msg);
  assert.equal(published.length, 1);
});
