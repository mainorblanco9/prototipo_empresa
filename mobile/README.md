# Apps Android e iOS (Capacitor)

Envoltorio nativo de la PWA: la app abre `/facturas/movil/` servida por Django.

## Pasos

1. `cd mobile; npm install`
2. Ajusta `server.url` en `capacitor.config.json`:
   - Emulador Android: `http://10.0.2.2:8000/facturas/movil/` (ya configurado).
   - Teléfono físico / iOS: `http://<IP-de-tu-PC>:8000/facturas/movil/` y arranca Django con
     `DJANGO_ALLOWED_HOSTS=<IP-de-tu-PC>` y `python manage.py runserver 0.0.0.0:8000`.
3. `npm run add:android` y/o `npm run add:ios` (iOS requiere macOS con Xcode), luego `npm run sync`.
4. `npm run open:android` (Android Studio) o `npm run open:ios` (Xcode) y ejecuta la app.
5. iOS con `http://`: agrega una excepción App Transport Security en `Info.plist`
   (`NSAllowsLocalNetworking`). Para producción usa HTTPS y quita `cleartext`.

## Notas
- Las facturas capturadas se guardan en IndexedDB del WebView y se sincronizan al reconectar.
  El service worker (modo sin conexión de la pantalla) solo funciona en contextos seguros
  (HTTPS o `localhost`); con HTTP en la red local la captura funciona, pero la pantalla no se
  abre sin conexión.
- Inicia sesión una vez en la app (usuario de Django) para poder sincronizar.
- El envío a Hacienda se hace desde la computadora (`/facturas/emitidas/`).
