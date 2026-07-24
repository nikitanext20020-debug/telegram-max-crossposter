const token = process.env.TELEGRAM_BOT_TOKEN?.trim();
if (!token) {
  console.error("Добавьте новый TELEGRAM_BOT_TOKEN в .env");
  process.exit(1);
}

const response = await fetch(
  `https://api.telegram.org/bot${token}/getWebhookInfo`,
);
const data = await response.json().catch(() => ({}));
if (!response.ok || !data.ok) {
  console.error(data.description || `Telegram вернул HTTP ${response.status}`);
  process.exit(1);
}

const info = data.result;
console.log(
  JSON.stringify(
    {
      url: info.url,
      pending_update_count: info.pending_update_count,
      last_error_date: info.last_error_date,
      last_error_message: info.last_error_message,
      allowed_updates: info.allowed_updates,
    },
    null,
    2,
  ),
);
