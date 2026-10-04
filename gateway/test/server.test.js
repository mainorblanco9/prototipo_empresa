import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createApp } from "../src/server.js";
import { createHaciendaClient } from "../src/hacienda-client.js";
import { createMockHacienda } from "../src/mock-hacienda.js";

const ID = "11111111-1111-1111-1111-111111111111";
const CLAVE = "50604102600310100000000100001010000000001112345678";

function payload(extra = "") {
  const xml = `<FacturaElectronica><Clave>${CLAVE}</Clave>${extra}</FacturaElectronica>`;
  return {
    clave: CLAVE,
    fecha: "2026-10-04T12:00:00-06:00",
    emisor: { tipoIdentificacion: "02", numeroIdentificacion: "3101000000" },
    receptor: { tipoIdentificacion: "01", numeroIdentificacion: "100000000" },
    comprobanteXml: Buffer.from(xml).toString("base64"),
  };
}

function setup(invoiceStatus, xmlExtra = "") {
  const mock = createMockHacienda();
  const calls = [];
  const fakeFetch = async (url, init = {}) => {
    const u = new URL(url);
    if (u.host === "hacienda") {
      const r = mock.handle({
        method: init.method ?? "GET",
        path: u.pathname,
        headers: Object.fromEntries(
          Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]),
        ),
        body: init.body ?? "",
      });
      return new Response(r.body === null ? null : JSON.stringify(r.body), {
        status: r.status,
        headers: r.headers,
      });
    }
    calls.push(`${init.method ?? "GET"} ${u.pathname}`);
    const reply = (body) => new Response(JSON.stringify(body), { status: 200 });
    if (u.pathname.endsWith("/queue/")) {
      return reply({ id: ID, status: "queued", version: 2, clave: CLAVE });
    }
    if (u.pathname.endsWith("/reception-payload/")) return reply(payload(xmlExtra));
    if (u.pathname.endsWith("/hacienda-result/")) {
      return reply({ status: JSON.parse(init.body).state === "aceptado" ? "accepted" : "rejected" });
    }
    return reply({ id: ID, status: invoiceStatus, version: 1, clave: invoiceStatus === "ready" ? "" : CLAVE });
  };
  const hacienda = createHaciendaClient({
    baseUrl: "http://hacienda",
    tokenUrl: "http://hacienda/token",
    fetchImpl: fakeFetch,
  });
  const server = createServer(
    createApp({ djangoUrl: "http://django", hacienda, mock, fetchImpl: fakeFetch, pollIntervalMs: 1 }),
  );
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () =>
      resolve({ server, calls, url: `http://127.0.0.1:${server.address().port}` }),
    ),
  );
}

const submit = (url) =>
  fetch(`${url}/invoices/${ID}/submit`, { method: "POST", headers: { Authorization: "Token abc" } });

test("requiere token", async () => {
  const { server, url } = await setup("ready");
  assert.equal((await fetch(`${url}/invoices`)).status, 401);
  server.close();
});

test("factura lista termina aceptada", async () => {
  const { server, url, calls } = await setup("ready");
  const r = await submit(url);
  const body = await r.json();
  assert.equal(r.status, 200);
  assert.equal(body.hacienda_state, "aceptado");
  assert.equal(body.invoice_status, "accepted");
  assert.ok(calls.some((c) => c.endsWith("/queue/")));
  server.close();
});

test("el simulador puede rechazar", async () => {
  const { server, url } = await setup("ready", "<Detalle>RECHAZAR</Detalle>");
  const body = await (await submit(url)).json();
  assert.equal(body.hacienda_state, "rechazado");
  assert.equal(body.invoice_status, "rejected");
  server.close();
});

test("rechaza borradores", async () => {
  const { server, url } = await setup("draft");
  assert.equal((await submit(url)).status, 409);
  server.close();
});

test("el simulador valida clave y evita duplicados", async () => {
  const mock = createMockHacienda();
  const token = mock.handle({
    method: "POST",
    path: "/token",
    body: "grant_type=password&username=u&password=p",
  }).body.access_token;
  const headers = { authorization: `Bearer ${token}` };
  const bad = mock.handle({ method: "POST", path: "/recepcion", headers, body: JSON.stringify({ ...payload(), clave: "1" }) });
  assert.equal(bad.status, 400);
  const ok = mock.handle({ method: "POST", path: "/recepcion", headers, body: JSON.stringify(payload()) });
  assert.equal(ok.status, 202);
  const dup = mock.handle({ method: "POST", path: "/recepcion", headers, body: JSON.stringify(payload()) });
  assert.equal(dup.status, 400);
  assert.equal(mock.handle({ method: "GET", path: `/recepcion/${CLAVE}` }).status, 401);
});

test("modo real no disponible", () => {
  assert.throws(() => createHaciendaClient({ mode: "production" }), /XAdES/);
});
