import json

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#0b6bff"/><stop offset="1" stop-color="#00c2a8"/></linearGradient></defs>'
    '<rect width="64" height="64" rx="17" fill="url(#g)"/>'
    '<path d="M42.5 20.5A15 15 0 1 0 47 32H36" fill="none" stroke="#fff" '
    'stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M25 32.5l5.2 5.2 9.3-10.2" fill="none" stroke="#bdfff0" '
    'stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)

SERVICE_WORKER_JS = """
const CACHE = "guanabill-shell-v2";
const CAPTURE_URL = "/facturas/movil/";
const OFFLINE_URL = "/offline/";

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll([OFFLINE_URL, "/icon.svg"]))
  );
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key !== CACHE).map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

// Solo se guardan la pantalla sin conexión, el ícono y el formulario de captura
// móvil (sin datos de facturas; estas viven en IndexedDB del dispositivo).
// Las demás páginas con datos nunca se almacenan en caché.
self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET" || request.mode !== "navigate") return;
  const isCapture = new URL(request.url).pathname === CAPTURE_URL;
  event.respondWith(
    fetch(request)
      .then(response => {
        if (isCapture && response.ok && !response.redirected) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(CAPTURE_URL, copy));
        }
        return response;
      })
      .catch(() => Promise.resolve(isCapture ? caches.match(CAPTURE_URL) : null)
        .then(cached => cached || caches.match(OFFLINE_URL)))
  );
});
""".strip()


@require_GET
def manifest(request):
    data = {
        "name": "GuanaBill · Facturación electrónica",
        "short_name": "GuanaBill",
        "description": (
            "Gestiona y envía facturas electrónicas a Hacienda: por API, Excel o factura a factura."
        ),
        "lang": "es-CR",
        "start_url": "/facturas/movil/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f2f6fb",
        "theme_color": "#2359d1",
        "shortcuts": [
            {"name": "Capturar factura", "url": "/facturas/movil/"},
            {"name": "Facturas emitidas", "url": "/facturas/emitidas/"},
        ],
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }
    return HttpResponse(
        json.dumps(data, ensure_ascii=False),
        content_type="application/manifest+json; charset=utf-8",
    )


@never_cache
@require_GET
def service_worker(request):
    response = HttpResponse(
        SERVICE_WORKER_JS,
        content_type="application/javascript; charset=utf-8",
    )
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
def icon(request):
    return HttpResponse(ICON_SVG, content_type="image/svg+xml")


@require_GET
def offline(request):
    return render(request, "validador/offline.html")
