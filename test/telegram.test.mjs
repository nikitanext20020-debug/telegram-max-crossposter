import test from "node:test";
import assert from "node:assert/strict";
import { extractTelegramMedia, messageText } from "../src/telegram.mjs";

test("chooses the largest Telegram photo", () => {
  const media = extractTelegramMedia({
    message_id: 42,
    photo: [
      { file_id: "small", width: 90, height: 90, file_size: 1000 },
      { file_id: "large", width: 1280, height: 720, file_size: 100000 },
    ],
  });

  assert.deepEqual(media, [
    { type: "image", fileId: "large", fileName: "photo-42.jpg" },
  ]);
});

test("extracts video and document with MAX-compatible categories", () => {
  const media = extractTelegramMedia({
    message_id: 7,
    video: { file_id: "video-id" },
    document: { file_id: "doc-id", file_name: "report.pdf" },
  });

  assert.deepEqual(media, [
    { type: "video", fileId: "video-id", fileName: "video-7.mp4" },
    { type: "file", fileId: "doc-id", fileName: "report.pdf" },
  ]);
});

test("renders poll as readable text", () => {
  assert.deepEqual(
    messageText({
      poll: {
        question: "Выбираем?",
        options: [{ text: "Да" }, { text: "Нет" }],
      },
    }),
    { text: "Выбираем?\n\n• Да\n• Нет", entities: [] },
  );
});
