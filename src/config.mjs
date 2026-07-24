const MAX_API_BASE = "https://platform-api2.max.ru";

function required(env, name) {
  const value = env[name]?.trim();
  if (!value) throw new Error(`Не задана обязательная переменная ${name}`);
  return value;
}

function booleanValue(value, fallback) {
  if (value == null || value === "") return fallback;
  return !["0", "false", "no", "off"].includes(value.toLowerCase());
}

function parseChatIds(value) {
  return new Set(
    (value ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  );
}

export function loadConfig(env = process.env) {
  const webhookSecret = required(env, "TELEGRAM_WEBHOOK_SECRET");
  if (!/^[A-Za-z0-9_-]{1,256}$/.test(webhookSecret)) {
    throw new Error(
      "TELEGRAM_WEBHOOK_SECRET может содержать только A-Z, a-z, 0-9, _ и -",
    );
  }

  return {
    telegramToken: required(env, "TELEGRAM_BOT_TOKEN"),
    maxToken: required(env, "MAX_BOT_TOKEN"),
    maxChatId: required(env, "MAX_CHAT_ID"),
    webhookSecret,
    sourceChatIds: parseChatIds(env.TELEGRAM_SOURCE_CHAT_IDS),
    signatureText: env.MAX_SIGNATURE_TEXT?.trim() ?? "",
    signatureUrl: env.MAX_SIGNATURE_URL?.trim() ?? "",
    maxNotify: booleanValue(env.MAX_NOTIFY, true),
    disableLinkPreview: booleanValue(env.MAX_DISABLE_LINK_PREVIEW, false),
    maxApiBase: (env.MAX_API_BASE?.trim() || MAX_API_BASE).replace(/\/+$/, ""),
    port: Number.parseInt(env.PORT || "8000", 10),
  };
}

export function sourceChatAllowed(config, chatId) {
  return (
    config.sourceChatIds.size === 0 ||
    config.sourceChatIds.has(String(chatId))
  );
}
