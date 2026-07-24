import test from "node:test";
import assert from "node:assert/strict";
import { crosspostMessages } from "../src/crosspost.mjs";

test("publishes formatted text and media with signature", async () => {
  const calls = [];
  const fakeFetch = async (url, init = {}) => {
    const target = String(url);
    if (target.includes("getFile")) {
      return Response.json({
        ok: true,
        result: { file_path: "photos/photo.jpg", file_size: 4 },
      });
    }
    if (target.includes("/file/bot")) {
      return new Response(new Blob(["test"], { type: "image/jpeg" }), {
        status: 200,
      });
    }
    if (target.includes("/uploads?type=image")) {
      return Response.json({ url: "https://upload.test/image", token: "slot" });
    }
    if (target === "https://upload.test/image") {
      return Response.json({
        photos: { "100x100": { token: "uploaded-image" } },
      });
    }
    if (target.includes("/messages?")) {
      calls.push(JSON.parse(init.body));
      return Response.json({ message: { id: "max-message-1" } });
    }
    throw new Error(`Unexpected URL in test: ${target}`);
  };

  await crosspostMessages(
    {
      telegramToken: "telegram-token",
      maxToken: "max-token",
      maxChatId: "123",
      signatureText: "Подписаться",
      signatureUrl: "https://example.com",
      maxNotify: true,
      disableLinkPreview: false,
    },
    [
      {
        message_id: 1,
        chat: { id: -100 },
        text: "Привет",
        entities: [{ type: "bold", offset: 0, length: 6 }],
        photo: [{ file_id: "photo-id", width: 100, height: 100, file_size: 4 }],
      },
    ],
    { externalFetch: fakeFetch, maxFetch: fakeFetch },
  );

  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].text,
    '<b>Привет</b>\n\n<a href="https://example.com/">Подписаться</a>',
  );
  assert.deepEqual(calls[0].attachments, [
    {
      type: "image",
      payload: { photos: { "100x100": { token: "uploaded-image" } } },
    },
  ]);
});

test("album of several photos becomes a single MAX post", async () => {
  const calls = [];
  let uploaded = 0;
  const fakeFetch = async (url, init = {}) => {
    const target = String(url);
    if (target.includes("getFile")) {
      return Response.json({
        ok: true,
        result: { file_path: "photos/photo.jpg", file_size: 4 },
      });
    }
    if (target.includes("/file/bot")) {
      return new Response(new Blob(["test"], { type: "image/jpeg" }), {
        status: 200,
      });
    }
    if (target.includes("/uploads?type=image")) {
      return Response.json({ url: "https://upload.test/image", token: "slot" });
    }
    if (target === "https://upload.test/image") {
      uploaded += 1;
      return Response.json({
        photos: { [`photo-${uploaded}`]: { token: `uploaded-${uploaded}` } },
      });
    }
    if (target.includes("/messages?")) {
      calls.push(JSON.parse(init.body));
      return Response.json({ message: { id: "max-message-1" } });
    }
    throw new Error(`Unexpected URL in test: ${target}`);
  };

  await crosspostMessages(
    {
      telegramToken: "telegram-token",
      maxToken: "max-token",
      maxChatId: "123",
      signatureText: "Подписаться",
      signatureUrl: "https://example.com",
      maxNotify: true,
      disableLinkPreview: false,
    },
    [
      // Telegram puts the caption on the first album item only.
      {
        message_id: 10,
        chat: { id: -100 },
        media_group_id: "album-1",
        caption: "Общая подпись",
        photo: [{ file_id: "photo-a", width: 100, height: 100, file_size: 4 }],
      },
      {
        message_id: 11,
        chat: { id: -100 },
        media_group_id: "album-1",
        photo: [{ file_id: "photo-b", width: 100, height: 100, file_size: 4 }],
      },
    ],
    { externalFetch: fakeFetch, maxFetch: fakeFetch },
  );

  // One MAX message, not one per photo.
  assert.equal(calls.length, 1);
  // Both photos are attached to that single message.
  assert.equal(calls[0].attachments.length, 2);
  assert.ok(calls[0].attachments.every((a) => a.type === "image"));
  // The caption from the first item is preserved, plus the signature.
  assert.equal(
    calls[0].text,
    'Общая подпись\n\n<a href="https://example.com/">Подписаться</a>',
  );
});
