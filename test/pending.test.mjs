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
