const token = process.env.MAX_BOT_TOKEN?.trim();
const base = (process.env.MAX_API_BASE || "https://platform-api2.max.ru").replace(
  /\/+$/,
  "",
);

if (!token) {
  console.error("Добавьте новый MAX_BOT_TOKEN в .env");
  process.exit(1);
}

async function get(path) {
  const response = await maxApiFetch(`${base}${path}`, {
    headers: { authorization: token },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || `MAX API вернул HTTP ${response.status}`);
  }
  return data;
}

try {
  const me = await get("/me");
  console.log(`MAX-бот: ${me.name || me.username || me.user_id}`);

  const data = await get(
    "/updates?timeout=0&types=bot_added,message_created,chat_title_changed",
  );
  const update = [...(data.updates ?? [])]
    .reverse()
    .find((item) => item.is_channel === true || item.chat_id);

  if (!update?.chat_id) {
    console.error(
      [
        "ID канала пока не найден.",
        "1. Убедитесь, что у MAX-бота не настроен другой webhook.",
        "2. Назначьте бота администратором MAX-канала.",
        "3. Если бот уже администратор — удалите и добавьте его снова.",
        "4. Сразу повторите: npm run discover:max",
      ].join("\n"),
    );
    process.exit(2);
  }

  console.log(`MAX_CHAT_ID=${update.chat_id}`);
  console.log("Скопируйте эту строку в .env и в настройки хостинга.");
} catch (error) {
  console.error(`Не удалось получить MAX chat_id: ${error.message}`);
  process.exit(1);
}
import { maxApiFetch } from "../src/max-api-fetch.mjs";
