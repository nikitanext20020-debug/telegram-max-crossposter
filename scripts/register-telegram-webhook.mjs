const token = process.env.TELEGRAM_BOT_TOKEN?.trim();
const publicUrl = process.env.PUBLIC_URL?.trim()?.replace(/\/+$/, "");
const secret = process.env.TELEGRAM_WEBHOOK_SECRET?.trim();

if (!token || !publicUrl || !secret) {
  console.error(
    "В .env должны быть TELEGRAM_BOT_TOKEN, PUBLIC_URL и TELEGRAM_WEBHOOK_SECRET",
  );
  process.exit(1);
}
if (!/^https:\/\//.test(publicUrl)) {
  console.error("PUBLIC_URL должен начинаться с https://");
  process.exit(1);
}
if (!/^[A-Za-z0-9_-]{1,256}$/.test(secret)) {
  console.error("TELEGRAM_WEBHOOK_SECRET содержит недопустимые символы");
  process.exit(1);
}

const response = await fetch(
  `https://api.telegram.org/bot${token}/setWebhook`,
  {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      url: `${publicUrl}/telegram`,
      secret_token: secret,
      allowed_updates: ["channel_post"],
      drop_pending_updates: false,
      max_connections: 20,
    }),
  },
);
const data = await response.json().catch(() => ({}));
if (!response.ok || !data.ok) {
  console.error(data.description || `Telegram вернул HTTP ${response.status}`);
  process.exit(1);
}

console.log("Telegram webhook подключён.");
console.log(`Адрес: ${publicUrl}/telegram`);
