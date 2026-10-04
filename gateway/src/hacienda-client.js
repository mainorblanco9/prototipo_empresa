// Cliente de la API de recepción de Hacienda. Solo se habilita el simulador
// local: enviar a Hacienda real exige XML firmado con XAdES-EPES y el
// certificado del contribuyente, algo que este prototipo aún no implementa.
export function createHaciendaClient({
  mode = "local",
  baseUrl,
  tokenUrl,
  username = "local-user",
  password = "local-pass",
  clientId = "api-stag",
  fetchImpl = fetch,
}) {
  if (mode !== "local") {
    throw new Error(
      `HACIENDA_MODE="${mode}" no está disponible: falta la firma XAdES del XML y el certificado.`,
    );
  }
  let token = null;
  let expiresAt = 0;

  async function accessToken() {
    if (token && Date.now() < expiresAt - 5000) return token;
    const response = await fetchImpl(tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "password",
        client_id: clientId,
        username,
        password,
      }).toString(),
    });
    if (!response.ok) throw new Error(`Hacienda rechazó la autenticación (${response.status}).`);
    const data = await response.json();
    token = data.access_token;
    expiresAt = Date.now() + (data.expires_in ?? 300) * 1000;
    return token;
  }

  async function call(path, init = {}) {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${await accessToken()}`, ...(init.headers ?? {}) },
    });
    return response;
  }

  return {
    mode,
    async send(payload) {
      const response = await call("/recepcion", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (response.status !== 202) {
        const cause = response.headers.get("x-error-cause") ?? `HTTP ${response.status}`;
        throw Object.assign(new Error(`Hacienda no recibió el comprobante: ${cause}`), { status: 422 });
      }
    },
    async status(clave) {
      const response = await call(`/recepcion/${clave}`);
      if (!response.ok) {
        throw Object.assign(new Error(`Consulta de estado falló (${response.status}).`), { status: 502 });
      }
      return response.json();
    },
  };
}

export function decodeMessage(respuestaXml) {
  if (!respuestaXml) return "";
  const xml = Buffer.from(respuestaXml, "base64").toString("utf8");
  return /<DetalleMensaje>([^<]*)<\/DetalleMensaje>/.exec(xml)?.[1] ?? "";
}
