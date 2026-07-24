import test from "node:test";
import assert from "node:assert/strict";
import {
  buildSignatureHtml,
  splitTelegramText,
  telegramTextToMaxHtml,
} from "../src/format.mjs";

test("converts Telegram formatting and escapes HTML", () => {
  const text = "Жирно & ссылка";
  const html = telegramTextToMaxHtml(text, [
    { type: "bold", offset: 0, length: 5 },
    {
      type: "text_link",
      offset: 8,
      length: 6,
      url: "https://example.com/path?a=1&b=2",
    },
  ]);

  assert.equal(
    html,
    '<b>Жирно</b> &amp; <a href="https://example.com/path?a=1&amp;b=2">ссылка</a>',
  );
});

test("keeps UTF-16 Telegram entity offsets for emoji", () => {
  const text = "😀 тест";
  const html = telegramTextToMaxHtml(text, [
    { type: "bold", offset: 3, length: 4 },
  ]);
  assert.equal(html, "😀 <b>тест</b>");
});

test("splits text without breaking a surrogate pair", () => {
  const chunks = splitTelegramText("1234😀5678", [], 5);
  assert.equal(chunks.map((chunk) => chunk.text).join(""), "1234😀5678");
  assert.ok(!/[\uD800-\uDBFF]$/.test(chunks[0].text));
});

test("builds a safe linked signature", () => {
  assert.equal(
    buildSignatureHtml("Наш сайт", "https://example.com/a?x=1&y=2"),
    '\n\n<a href="https://example.com/a?x=1&amp;y=2">Наш сайт</a>',
  );
  assert.throws(
    () => buildSignatureHtml("Плохая", "javascript:alert(1)"),
    /http/,
  );
});
