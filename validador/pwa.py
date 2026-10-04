import json

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<rect width="512" height="512" rx="112" fill="#2359d1"/>'
    '<path d="M150 150h212v212H150z" fill="none" stroke="#fff" '
    'stroke-width="28" stroke-linejoin="round"/>'
    '<path d="M196 256l44 44 80-88" fill="none" stroke="#00d1a4" '
    'stroke-width="32" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)

SERVICE_WORKER_JS = """
const CACHE = "analizadorpro-shell-v1";
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

// Solo se guarda la pantalla sin conexión y el ícono. Las páginas con datos
// de facturas nunca se almacenan en caché del navegador.
self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET" || request.mode !== "navigate") return;
  event.respondWith(
    fetch(request).catch(() => caches.match(OFFLINE_URL))
  );
});
""".strip()


@require_GET
def manifest(request):
    data = {
        "name": "AnalizadorPro · Facturación local",
        "short_name": "AnalizadorPro",
        "description": (
            "Prototipo local para preparar y dar seguimiento a facturas en "
            "colones. No transmite documentos a Hacienda."
        ),
        "lang": "es-CR",
        "start_url": "/facturas/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f2f6fb",
        "theme_color": "#2359d1",
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
