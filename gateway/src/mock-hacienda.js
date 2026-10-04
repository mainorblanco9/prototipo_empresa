import { randomBytes } from "node:crypto";

// Simulador local de la API de recepción de Hacienda (ATV).
// Reproduce el contrato público: token OAuth2 (password grant),
// POST /recepcion -> 202 y GET /recepcion/{clave} -> ind-estado con
// recibido | procesando | aceptado | rechazado y respuesta-xml en base64.
// No valida firma XAdES ni esquema XSD; no es fiscal.

const CLAVE = /^\d{50}$/;
const ID_TYPES = new Set(["01", "02", "03", "04", "05", "06"]);

function json(status, body, headers = {}) {
  return { status, headers: { "Content-Type": "application/json", ...headers }, body };
}

function reject(cause) {
  return json(400, null, { "X-Error-Cause": cause });
}

function mensajeHacienda({ clave, accepted, detail }) {
  const code = accepted ? "1" : "3";
  return Buffer.from(
    `<?xml version="1.0" encoding="utf-8"?><MensajeHacienda><Clave>${clave}</Clave>` +
      `<Mensaje>${code}</Mensaje><DetalleMensaje>${detail}</DetalleMensaje></MensajeHacienda>`,
  ).toString("base64");
}

export function createMockHacienda() {
  const receipts = new Map();
  const tokens = new Set();

  function authorized(headers) {
    const match = /^Bearer (.+)$/.exec(headers.authorization ?? "");
    return Boolean(match && tokens.has(match[1]));
  }

  return {
    receipts,
    handle({ method, path: rawPath, headers = {}, body = "" }) {
      // Mismas rutas que ATV: IDP de Keycloak y /recepcion/v1/recepcion.
      const path = rawPath
        .replace(/^\/auth\/realms\/[^/]+\/protocol\/openid-connect\/token$/, "/token")
        .replace(/^\/recepcion\/v1\/recepcion/, "/recepcion");
      if (method === "POST" && path === "/token") {
        const form = new URLSearchParams(body);
        if (form.get("grant_type") !== "password" || !form.get("username") || !form.get("password")) {
          return json(400, { error: "invalid_request" });
        }
        const accessToken = randomBytes(24).toString("hex");
        tokens.add(accessToken);
        return json(200, {
          access_token: accessToken,
          expires_in: 300,
          token_type: "bearer",
          scope: "",
        });
      }

      if (!authorized(headers)) return json(401, { error: "unauthorized" });

      if (method === "POST" && path === "/recepcion") {
        let data;
        try {
          data = JSON.parse(body);
        } catch {
          return reject("El cuerpo no es JSON válido");
        }
        if (!CLAVE.test(data?.clave ?? "")) return reject("La clave debe tener 50 dígitos");
        if (!data.fecha || Number.isNaN(Date.parse(data.fecha))) return reject("Fecha inválida");
        for (const side of ["emisor", "receptor"]) {
          const party = data[side];
          if (!ID_TYPES.has(party?.tipoIdentificacion) || !/^\d{9,12}$/.test(party?.numeroIdentificacion ?? "")) {
            return reject(`Datos de ${side} inválidos`);
          }
        }
        let xml;
        try {
          xml = Buffer.from(data.comprobanteXml ?? "", "base64").toString("utf8");
        } catch {
          xml = "";
        }
        if (!xml.includes("<FacturaElectronica") || !xml.includes(data.clave)) {
          return reject("comprobanteXml no corresponde a la clave enviada");
        }
        if (receipts.has(data.clave)) return reject("El comprobante ya fue recibido");
        receipts.set(data.clave, {
          clave: data.clave,
          fecha: data.fecha,
          polls: 0,
          wantsReject: xml.includes("RECHAZAR"),
        });
        return json(202, null, { Location: `/recepcion/${data.clave}` });
      }

      const match = method === "GET" && /^\/recepcion\/(\d{50})$/.exec(path);
      if (match) {
        const receipt = receipts.get(match[1]);
        if (!receipt) return json(404, null, { "X-Error-Cause": "Comprobante no encontrado" });
        receipt.polls += 1;
        const base = { clave: receipt.clave, fecha: receipt.fecha };
        if (receipt.polls === 1) return json(200, { ...base, "ind-estado": "procesando" });
        const accepted = !receipt.wantsReject;
        return json(200, {
          ...base,
          "ind-estado": accepted ? "aceptado" : "rechazado",
          "respuesta-xml": mensajeHacienda({
            clave: receipt.clave,
            accepted,
            detail: accepted
              ? "Comprobante aceptado (simulador local)"
              : "Comprobante rechazado (simulador local)",
          }),
        });
      }

      return json(404, { error: "not_found" });
    },
  };
}
