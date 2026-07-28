const messageKey = (message) => `${message.chat.id}:${message.message_id}`;

export function createPendingQueue({
  delayMs,
  onReady,
  setTimeoutImpl = setTimeout,
  clearTimeoutImpl = clearTimeout,
}) {
  const pending = new Map();
  const published = new Set();

  function rememberPublished(key) {
    published.add(key);
    if (published.size > 5000) {
      published.delete(published.values().next().value);
    }
  }

  function enqueue(message) {
    const key = messageKey(message);
    if (published.has(key)) return Promise.resolve({ ignored: "already_published" });

    const existing = pending.get(key);
    if (existing) clearTimeoutImpl(existing.timer);

    const entry = existing ?? { waiters: [], generation: 0 };
    entry.message = message;
    entry.generation += 1;
    const generation = entry.generation;

    const result = new Promise((resolve, reject) => {
      entry.waiters.push({ resolve, reject });
    });

    entry.timer = setTimeoutImpl(async () => {
      if (
        pending.get(key) !== entry ||
        entry.generation !== generation
      ) {
        return;
      }

      pending.delete(key);
      rememberPublished(key);
      try {
        const value = await onReady(entry.message);
        for (const waiter of entry.waiters) waiter.resolve(value);
      } catch (error) {
        published.delete(key);
        for (const waiter of entry.waiters) waiter.reject(error);
      }
    }, delayMs);
    pending.set(key, entry);

    return result;
  }

  return { enqueue };
}
