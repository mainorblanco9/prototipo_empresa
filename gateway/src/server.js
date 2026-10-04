import { createServer } from "node:http";
import { pathToFileURL } from "node:url";
import { createHaciendaClient, decodeMessage } from "./hacienda-client.js";
import { createMockHacienda } from "./mock-hacienda.js";

const UUID = "[0-9a-fA-F-]{36}";
const MAX_BODY = 1_000_000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        reject(Object.assign(new Error("Cuerpo demasiado grande"), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

async function readJson(req) {
  const text = await readBody(req);
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw Object.assign(new Error("JSON inválido"), { status: 400 });
  }
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", ...headers });
  res.end(body === null || body === undefined ? undefined : JSON.stringify(body));
}

export function createApp({
  djangoUrl,
  hacienda,
  mock = createMockHacienda(),
  fetchImpl = fetch,
  pollIntervalMs = 300,
  maxPolls = 10,
}) {
  const base = djangoUrl.replace(/\/+$/, "");

  async function django(path, { method = "GET", auth, body } = {}) {
    const headers = { Accept: "application/json" };
    if (auth) headers.Authorization = auth;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetchImpl(`${base}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { detail: text.slice(0, 200) };
    }
    return { status: response.status, data };
  }

  async function submit(id, auth) {
    const api = `/api/v1/invoices/${id}`;
    let detail = await django(`${api}/`, { auth });
    if (detail.status !== 200) return detail;
    let invoice = detail.data;
    if (invoice.status === "ready") {
      const queued = await django(`${api}/queue/`, {
        method: "POST",
        auth,
        body: { version: invoice.version },
      });
      if (queued.status !== 200) return queued;
      invoice = queued.data;
    } else if (!["queued", "processing"].includes(invoice.status)) {
      return {
        status: 409,
        data: {
          detail: "Solo las facturas en estado 'ready' pueden enviarse.",
          status: invoice.status,
        },
      };
    }

    const payload = await django(`${api}/reception-payload/`, { auth });
    if (payload.status !== 200) return payload;

    try {
      await hacienda.send(payload.data);
    } catch (error) {
      if (!/ya fue recibido/.test(error.message)) {
        return { status: error.status ?? 502, data: { detail: error.message } };
      }
    }

    let state = { "ind-estado": "recibido" };
    for (let attempt = 0; attempt < maxPolls; attempt += 1) {
      await sleep(pollIntervalMs);
      state = await hacienda.status(invoice.clave);
      if (["aceptado", "rechazado"].includes(state["ind-estado"])) break;
    }

    const indicator = state["ind-estado"];
    const message = decodeMessage(state["respuesta-xml"]);
    if (indicator === "recibido") {
      return { status: 200, data: { hacienda_state: indicator, clave: invoice.clave } };
    }
    const saved = await django(`${api}/hacienda-result/`, {
      method: "POST",
      auth,
      body: { state: indicator, message },
    });
    if (saved.status !== 200) return saved;
    return {
      status: 200,
      data: {
        hacienda_state: indicator,
        clave: invoice.clave,
        message,
        invoice_status: saved.data.status,
      },
    };
  }

  return async function handler(req, res) {
    try {
      const url = new URL(req.url, "http://gateway");
      const path = url.pathname;

      if (path.startsWith("/mock/")) {
        const result = mock.handle({
          method: req.method,
          path: path.slice("/mock".length),
          headers: req.headers,
          body: await readBody(req),
        });
        return send(res, result.status, result.body, result.headers);
      }

      if (req.method === "GET" && path === "/health") {
        return send(res, 200, { status: "ok", hacienda_mode: hacienda.mode });
      }

      if (req.method === "POST" && path === "/auth/token") {
        const { username, password } = await readJson(req);
        const r = await django("/api/v1/auth/token/", {
          method: "POST",
          body: { username, password },
        });
        return send(res, r.status, r.data);
      }

      const auth = req.headers.authorization;
      if (!auth) return send(res, 401, { detail: "Falta Authorization: Token <token>." });

      if (req.method === "GET" && path === "/invoices") {
        const r = await django(`/api/v1/invoices/${url.search}`, { auth });
        return send(res, r.status, r.data);
      }

      const match = path.match(new RegExp(`^/invoices/(${UUID})/submit$`));
      if (req.method === "POST" && match) {
        const r = await submit(match[1], auth);
        return send(res, r.status, r.data);
      }

      return send(res, 404, { detail: "No encontrado." });
    } catch (error) {
      const status = error.status ?? 502;
      return send(res, status, {
        detail: status === 502 ? "No se pudo contactar a Django o a Hacienda." : error.message,
      });
    }
  };
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const port = Number(process.env.PORT ?? 3000);
  const djangoUrl = process.env.DJANGO_URL ?? "http://127.0.0.1:8000";
  const self = `http://127.0.0.1:${port}/mock`;
  const hacienda = createHaciendaClient({
    mode: process.env.HACIENDA_MODE ?? "local",
    baseUrl: process.env.HACIENDA_BASE_URL ?? self,
    tokenUrl: process.env.HACIENDA_TOKEN_URL ?? `${self}/token`,
    username: process.env.HACIENDA_USER,
    password: process.env.HACIENDA_PASSWORD,
  });
  createServer(createApp({ djangoUrl, hacienda })).listen(port, "127.0.0.1", () => {
    console.log(`Gateway en http://127.0.0.1:${port} -> ${djangoUrl} (Hacienda: ${hacienda.mode})`);
  });
}
