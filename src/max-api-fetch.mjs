import { readFileSync } from "node:fs";
import { Agent, request as httpsRequest } from "node:https";
import { rootCertificates } from "node:tls";

const maxRootCertificate = readFileSync(
  new URL("../certs/russian-trusted-root-ca.pem", import.meta.url),
  "utf8",
);

const maxAgent = new Agent({
  keepAlive: true,
  ca: [...rootCertificates, maxRootCertificate],
});

export function maxApiFetch(url, init = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    if (target.protocol !== "https:" || target.hostname !== "platform-api2.max.ru") {
      reject(new Error("Защищённый MAX-клиент разрешает только platform-api2.max.ru"));
      return;
    }

    const body =
      init.body == null
        ? undefined
        : Buffer.isBuffer(init.body)
          ? init.body
          : Buffer.from(String(init.body));

    const request = httpsRequest(
      target,
      {
        method: init.method || "GET",
        headers: init.headers,
        agent: maxAgent,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          resolve(
            new Response(Buffer.concat(chunks), {
              status: response.statusCode,
              statusText: response.statusMessage,
              headers: response.headers,
            }),
          );
        });
      },
    );
    request.on("error", reject);
    if (body) request.write(body);
    request.end();
  });
}
