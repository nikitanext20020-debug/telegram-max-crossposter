import { RemoteApiError } from "./telegram.mjs";
import { maxApiFetch } from "./max-api-fetch.mjs";

const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

async function parseResponse(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { message: text.slice(0, 300) };
  }
}

async function maxRequest(
  config,
  path,
  { method = "GET", body, fetchImpl = maxApiFetch } = {},
) {
  const response = await fetchImpl(`${config.maxApiBase}${path}`, {
    method,
    headers: {
      authorization: config.maxToken,
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await parseResponse(response);
  if (!response.ok || data.success === false) {
    throw new RemoteApiError(
      "MAX",
      data.message || data.error || `HTTP ${response.status}`,
      response.status,
      data.code,
    );
  }
  return data;
}

export async function uploadToMax(
  config,
  mediaType,
  blob,
  fileName,
  externalFetch = fetch,
  apiFetch = maxApiFetch,
) {
  const slot = await maxRequest(
    config,
    `/uploads?type=${encodeURIComponent(mediaType)}`,
    { method: "POST", fetchImpl: apiFetch },
  );
  if (!slot.url) throw new Error("MAX не вернул URL для загрузки файла");

  const form = new FormData();
  form.append("data", blob, fileName);
  const uploadResponse = await externalFetch(slot.url, {
    method: "POST",
    body: form,
  });
  const uploaded = await parseResponse(uploadResponse);
  if (!uploadResponse.ok) {
    throw new RemoteApiError(
      "MAX upload",
      uploaded.message || `HTTP ${uploadResponse.status}`,
      uploadResponse.status,
      uploaded.code,
    );
  }

  if (mediaType === "image" && uploaded.photos) {
    return { type: mediaType, payload: { photos: uploaded.photos } };
  }

  const token =
    uploaded.token ||
    uploaded.payload?.token ||
    uploaded.retval?.token ||
    slot.token;
  if (!token) throw new Error("MAX не вернул токен загруженного файла");

  return { type: mediaType, payload: { token } };
}

export async function sendMaxMessage(
  config,
  { text, attachments = [] },
  apiFetch = maxApiFetch,
) {
  const query = new URLSearchParams({
    chat_id: String(config.maxChatId),
    disable_link_preview: String(config.disableLinkPreview),
  });
  const body = {
    text: text || null,
    attachments: attachments.length > 0 ? attachments : null,
    notify: config.maxNotify,
    format: "html",
  };

  const delays = [0, 1000, 2000, 4000, 8000];
  let lastError;
  for (const delay of delays) {
    if (delay) await sleep(delay);
    try {
      return await maxRequest(config, `/messages?${query}`, {
        method: "POST",
        body,
        fetchImpl: apiFetch,
      });
    } catch (error) {
      lastError = error;
      if (error.code !== "attachment.not.ready") throw error;
    }
  }
  throw lastError;
}

export async function maxGet(config, path, fetchImpl = maxApiFetch) {
  return maxRequest(config, path, { fetchImpl });
}
