# GuanaBill: facturación electrónica para Hacienda

Aplicación inicial en Django para analizar tablas CSV/TSV/XLSX con encabezados dinámicos, mostrar una vista previa, recomendar y generar un gráfico, y descargar un reporte. Conserva reglas adicionales para planillas de gastos y la carga condicional a SharePoint.

## Requisitos

- Python 3.11 o posterior
- Ollama para usar IA localmente (opcional; la aplicación funciona sin IA)

## Ejecutar en Windows PowerShell

Desde esta carpeta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
$env:AI_PROVIDER = "ollama"
$env:OLLAMA_MODEL = "qwen2.5:3b"
python manage.py runserver
```

Instala Ollama para Windows desde <https://ollama.com/download/windows>. En otra
terminal, descarga e inicia el modelo una primera vez:

```powershell
ollama run qwen2.5:3b
```

Deja Ollama ejecutándose y levanta Django en la terminal del proyecto. La app se
conecta al servicio local en `http://localhost:11434`; no necesita una API key.
El análisis general de tablas y la recomendación de gráficos envían a Ollama
solo los encabezados y los tipos/conteos inferidos; los valores se procesan
localmente. La pantalla de facturas tiene su propio flujo de autodetección y,
solo cuando necesita interpretar encabezados desconocidos, envía a Ollama local
una muestra pequeña de valores (detallada más abajo). La descarga del modelo
requiere Internet y espacio en disco. El rendimiento depende de la memoria y
el procesador disponibles.

Abre <http://127.0.0.1:8000/> e inicia sesión con el usuario mock `demo` y la contraseña `Demo123!`. La cuenta mock solo se habilita con `DJANGO_DEBUG=true` (valor local predeterminado) y no debe usarse en producción. Puedes cambiar el usuario y la contraseña de desarrollo con `DEMO_USERNAME` y `DEMO_PASSWORD`.

Después del primer inicio de sesión, GuanaBill presenta un selector de estilo (minimalista, abstracto o corporativo). Puedes describir el ambiente visual deseado y pedir una recomendación; la IA selecciona y aplica automáticamente uno de los temas permitidos. La preferencia se guarda por usuario y se puede cambiar luego con “Cambiar diseño”. No se envían datos de archivos a esa recomendación.

Ollama local es el proveedor predeterminado. Configura `OLLAMA_BASE_URL` para cambiar su dirección y `OLLAMA_MODEL` para seleccionar otro modelo ya descargado. Para usar OpenAI en lugar del modelo local, configura `AI_PROVIDER=openai` y `OPENAI_API_KEY` en el entorno. Sin un proveedor de IA disponible, la validación determinista y la selección manual de tema siguen funcionando.

En Git Bash, activa el entorno con:

```bash
source .venv/Scripts/activate
```

## Análisis de tablas

- Acepta `.csv` UTF-8 con separador detectado automáticamente (coma, punto y coma, tabulador o barra vertical), `.tsv` y `.xlsx`.
- Detecta la fila de encabezados entre las primeras filas de cada hoja XLSX, ignora títulos combinados y selecciona automáticamente la hoja con la tabla más completa. Elimina ambigüedades en encabezados repetidos e infiere categorías, fechas, texto y números.
- Ollama recomienda qué columnas comparar. La aplicación calcula los agregados y dibuja un gráfico de barras de forma local; si Ollama no responde, usa una selección de respaldo basada en los tipos detectados y muestra un aviso.
- Reconoce flujos financieros con meses tanto en columnas como en filas. En archivos XLSX calcula localmente fórmulas aritméticas y sumas simples que no tengan resultados guardados por Excel. Si hay varias hojas anuales, puedes cambiar la hoja y el indicador del gráfico. La vista previa y la descarga corresponden a la hoja seleccionada automáticamente; los controles del gráfico responden al instante en el navegador, sin otra consulta a Ollama. Los meses mantienen su orden y los valores negativos se representan desde la línea de cero.
- Muestra una vista previa de hasta 200 filas; el análisis permite hasta 20.000 filas y 100 columnas. El reporte CSV conserva todas las filas y el enlace vence en 10 minutos.
- Prueba el flujo con [ejemplo_ventas.csv](./samples/ejemplo_ventas.csv), que usa punto y coma como separador.
- En el análisis general y la validación de gastos, los valores de celdas se procesan localmente. Para las facturas con encabezados desconocidos, consulta la nota de privacidad del flujo de autodetección más abajo. El flujo costarricense verifica fecha, proveedor, factura duplicada, moneda CRC, importes no negativos y suma subtotal + impuesto.
- La pantalla autenticada `/facturas/` y el endpoint `POST /api/invoices/parse/` detectan tablas de facturas en hojas y filas distintas de archivos XLSX/CSV/TSV, sin un tope fijo de columnas o filas en este flujo (el archivo está limitado a 10 MB), y convierten cada fila en un objeto JSON versionado. Si los encabezados no se reconocen, Ollama en `localhost` recibe todos los encabezados y una muestra de hasta 10 filas; la cantidad de filas de muestra se adapta al ancho de la tabla para mantener acotado el prompt, y cada valor se trunca a 100 caracteres. El resto del archivo se procesa en Django local. Este flujo no usa OpenAI ni envía datos fuera del computador; las tablas con encabezados reconocidos no requieren IA. Devuelve HTTP 200 cuando las facturas son válidas, HTTP 422 cuando requieren revisión y HTTP 503 si hace falta mapear encabezados pero Ollama no está disponible. Esta etapa solo prepara y valida datos; no transmite facturas a TribuCR.
- En desarrollo local puedes ejecutar una simulación técnica local desde `/facturas/modelo/` o `POST /api/invoices/local-simulation/`. Solo se habilita con `DEBUG=true`, requiere sesión autenticada y no llama a Hacienda ni genera acuses o recibos fiscales. `/api/invoices/mock-submit/` se conserva como alias legado.
- Sube el archivo original a SharePoint por Microsoft Graph solo si hay al menos una fila y todas las filas pasan las validaciones.
- Las tablas de otros temas no se cargan a SharePoint, pues no pasan por las reglas de validación de gastos.
- Si hay errores, configuración incompleta o una falla de Microsoft Graph, muestra el resultado y explica que no hubo una carga exitosa.
- Protege la página de carga y los reportes con una sesión Django; incluye una cuenta mock solo para desarrollo.
- Guarda por usuario la preferencia de tema de la interfaz; la recomendación IA se limita a temas visuales predefinidos y nunca modifica código.
- Permite usar Ollama local para recomendaciones de gráficos, sin enviar valores de celdas a un servicio externo.
- No escribe en un sistema contable ni sustituye revisión financiera/fiscal.
- SQLite es solo para desarrollo local.
- La bitácora autenticada `/facturas/bitacora/` persiste indefinidamente en la base local hasta que el usuario la borre. Solo conserva fecha, etapa, resultado, UUID aleatorio de lote y conteos; nunca guarda folios, proveedores, importes, contenido ni el nombre del archivo.

## Preparar objetos de facturas para TribuCR

En la aplicación, inicia sesión y abre **Preparar facturas** (ruta
`/facturas/`). Sube la plantilla XLSX/CSV/TSV; verás la validación, el mapeo
detectado (incluyendo la hoja y fila de encabezados) y el objeto JSON resultante.
La lectura detecta tablas con preámbulos y hojas distintas, procesando cualquier
número de filas y columnas dentro del límite de carga de 10 MB. Si los encabezados
no bastan para identificar los campos, Ollama local recibe todos los nombres de
columnas y una muestra variable de hasta 10 filas; el número de filas de muestra
se reduce al aumentar las columnas y cada celda se trunca a 100 caracteres. La
muestra puede incluir proveedor, folio o importes, pero solo se envía al servicio
Ollama en `localhost`; el resto del archivo se procesa en Django y no se llama a
OpenAI ni a servicios remotos. Para reducir errores de índices, Ollama devuelve
los nombres exactos de los encabezados y el servidor los valida; ante una
respuesta inválida, se solicita una corrección una vez. Si las columnas ya se
reconocen, no se invoca el modelo.

El endpoint local requiere una sesión autenticada y recibe `multipart/form-data`
con el campo `archivo`. Reutiliza las validaciones de gastos existentes y devuelve
un lote con `schema_version`, identificador aleatorio, alcance monetario CRC,
estado general y los objetos de factura extraídos. Cada objeto normaliza el
número, proveedor, fecha ISO, moneda e importes decimales;
los documentos que no pasan validación incluyen sus errores y quedan en
`needs_review`.
La página presenta una vista tabular del modelo (hasta 100 filas visibles),
permite expandir el JSON versionado y acepta separadores numéricos comunes
internacionales, por ejemplo `1.234,56` y `1,234.56`. En esta etapa se acepta
únicamente `CRC` (colón costarricense); cualquier otro código, incluidos USD y
MXN, queda en revisión y no se incluye en el modelo listo ni en la simulación.
El soporte para dólares queda para una etapa posterior. Incluye análisis
mensual por factura en colones; el gráfico local solo incluye filas validadas.
La segunda etapa se habilita únicamente para lotes válidos. Descarga
[ejemplo_facturas_octubre.csv](./samples/ejemplo_facturas_octubre.csv) para probar tres
facturas ficticias en CRC. Sus datos y la validación son exclusivamente de
demostración y no constituyen documentos ni certificación fiscal.

Tras validar el archivo, continúa a **Preparar modelo y ver trazabilidad**
(`/facturas/modelo/`). Esta segunda página muestra las etapas desde la recepción
del archivo hasta el modelo interno, grafica conteos de validación e importes
validados en CRC, y permite descargar el mismo lote como JSON. Ollama local
puede resumir los gráficos usando únicamente conteos y el total agregado en CRC; los
gráficos se calculan en Django y siguen disponibles aunque Ollama no responda.
El lote de trabajo se conserva durante 10 minutos; la bitácora de eventos es
durable en SQLite y almacena solo metadatos, no los datos de las facturas. La
etapa de envío queda pendiente hasta contar con el contrato oficial de TRIBU-CR.

### Bitácora local

`/facturas/bitacora/` registra análisis (incluidos los que requieren revisión),
preparación, descargas, respuestas de Ollama y simulaciones. Cada evento queda
vinculado a un UUID aleatorio y a la cuenta que lo generó. La bitácora no guarda
nombre de archivo, encabezados, folios, proveedores, importes ni filas de origen.
Los registros se conservan indefinidamente en la base local y se pueden borrar
desde esa pantalla; borrar el historial no elimina el lote temporal activo.

El área de facturas detecta hojas de flujo de caja con meses como columnas y
avisa cuando se carga por error un archivo financiero que no contiene facturas;
para graficar ese tipo de libro, usa el Analizador de datos (`/`).

### Simulación local del flujo

En `/facturas/`, analiza el archivo y elige un mes para revisar el gráfico de
totales por factura. Solo si todas las filas pasan las reglas internas CRC
se habilita el preparador en `/facturas/modelo/`. Desde esa etapa,
en desarrollo local, se puede simular el recorrido interno del mismo lote. El lote se
conserva durante 10 minutos en caché local y en la sesión para permitir recuperar
la preparación si el servidor de desarrollo se reinicia; la sesión no extiende
la expiración original. El mock devuelve estados
`simulation_passed` o `simulation_blocked` y un identificador `LOCAL-SIM-*`.
`simulation_passed` significa únicamente que el lote pasó los controles internos
actuales; no significa que Hacienda lo haya recibido, validado o aceptado. No se
generan claves, consecutivos, acuses ni recibos fiscales. Un lote que requiera
revisión queda bloqueado y la bitácora actualiza el resultado de la simulación.

Los clientes pueden llamar `POST /api/invoices/local-simulation/` con una sesión
autenticada, token CSRF y un archivo en el campo `archivo`, igual que el endpoint
de preparación. La ruta responde 404 cuando `DEBUG` está desactivado. La
respuesta declara `real_hacienda_contacted: false` y `real_submission: false`.
La simulación no contiene un cliente HTTP de remisión: ejecuta únicamente los
controles internos del lote.

El flujo actual analiza facturas recibidas de proveedores como gastos; no es un
sistema para emitir comprobantes a nombre del usuario. El esquema es una
representación interna mínima por fila, no un comprobante electrónico oficial
costarricense. No incluye todavía emisor/receptor, líneas de detalle, códigos
CABYS, actividad económica, firma digital ni estructura XML. La interfaz muestra
un catálogo de trabajo de factura electrónica, tiquete electrónico, notas de
crédito y débito, factura de compra y de exportación. Solo la primera tiene una
disponible y ninguna está implementada o validada para envío fiscal. Cada
tipo requiere su propio perfil de datos y reglas cotejados con documentación
oficial vigente; no se simulan como tipos compatibles por solo cambiarles el
nombre.

### Borrador local de una factura emitida

La ruta `/facturas/emitidas/factura-electronica/` es un flujo separado para
capturar localmente datos básicos de emisor, receptor y líneas en CRC. El precio
unitario se ingresa con IVA incluido; la persona selecciona la tarifa por línea
(13% predeterminada, 4%, 2%, 1%, 0% o exento) y el sistema extrae
automáticamente el impuesto incluido: `IVA = total_con_IVA × tarifa / (100 +
tarifa)`, redondeado a céntimos por línea. El subtotal antes de IVA es el total
con IVA menos ese impuesto. Así se arma un JSON interno 0.2.0. Las tarifas
disponibles son una ayuda de cálculo, no una determinación de la tarifa fiscal
aplicable. No asigna consecutivo, clave numérica, códigos tributarios ni tipos
de identificación oficiales; sus controles se limitan a campos obligatorios,
rangos, selección de tarifa y aritmética. El JSON descargable se guarda en caché
local durante 10 minutos y la bitácora solo almacena metadatos. No es un
comprobante válido ni un documento listo para emitir. Este borrador sirve para
probar la captura y el recorrido mientras se confirma el contrato oficial y sus
esquemas.

La página de emisión y la de análisis de facturas recibidas aparecen como flujos
independientes. El catálogo de tipos en el preparador es orientativo: la demo
genérica de factura electrónica no constituye soporte oficial y los restantes
perfiles siguen pendientes de requisitos vigentes.

Ejemplo para una sesión ya iniciada con su token CSRF:

```bash
curl -b cookies.txt -H "X-CSRFToken: $CSRF_TOKEN" \
  -F "archivo=@samples/ejemplo_gastos_validos.xlsx" \
  http://127.0.0.1:8000/api/invoices/parse/
```

La respuesta contiene objetos como:

```json
{
  "schema_version": "1.1.0",
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "currency_scope": "CRC",
  "status": "ready",
  "invoices": [
    {
      "invoice_number": "FAC-1001",
      "supplier": "Papelería Central",
      "issue_date": "2026-09-01",
      "currency": "CRC",
      "subtotal": "1000",
      "tax": "160",
      "total": "1160",
      "status": "validated",
      "errors": []
    }
  ],
  "integration": {
    "provider": "TribuCR",
    "status": "awaiting_official_api_contract",
    "accepted_currency": "CRC"
  }
}
```

Todavía no hay envío externo: no encontré una especificación pública oficial
de Hacienda para enviar o validar facturas electrónicas mediante API de
TRIBU-CR. La [página oficial de TRIBU-CR](https://www.hacienda.go.cr/TRIBU-CR.html)
ofrece información general y la [documentación pública de APIs de Hacienda](https://api.hacienda.go.cr/docs)
describe consultas, no el envío de facturas. Antes de añadir un cliente hacen
falta las especificaciones oficiales de autenticación, endpoints, esquemas,
ambiente de pruebas y tratamiento de reintentos. El Excel actual de gastos
representa una factura por fila; si TribuCR requiere líneas de detalle,
emisor/receptor, identificación tributaria, claves de actividad u otros campos,
se deberá ampliar el mapeo con esos datos, no inferirlos ni inventarlos.

## PWA instalable (móvil)

La app expone `/manifest.webmanifest`, `/sw.js`, `/icon.svg` y `/offline/`. En el
celular se puede instalar desde el navegador ("Añadir a pantalla de inicio") y
abre en `/facturas/` en modo standalone. El service worker solo guarda la pantalla
sin conexión y el ícono; las páginas con datos de facturas nunca se almacenan en
caché del navegador. Los navegadores solo permiten instalar PWAs bajo HTTPS o en
`localhost`; para probar desde un celular en la red local hace falta HTTPS (por
ejemplo con un túnel seguro) y agregar el host a `ALLOWED_HOSTS`.

## Configurar Microsoft Graph / SharePoint

Esta integración pertenece al Analizador general (`/`), no al flujo local de
preparación de facturas (`/facturas/`), que no envía archivos a SharePoint ni a
Hacienda. El Analizador general puede cargar una planilla de gastos a SharePoint
si se configuraron sus credenciales y todas las filas son válidas; si quieres
mantener todo local, usa el flujo `/facturas/` y no habilites esas credenciales.

La app usa autenticación de aplicación (client credentials) con Microsoft Entra ID. Registra una aplicación en el tenant de la empresa y configura el acceso de Microsoft Graph necesario para escribir únicamente en el sitio de SharePoint previsto. Para producción, prefiere el permiso de aplicación `Sites.Selected` con una concesión explícita de escritura al sitio objetivo, en lugar de permisos amplios sobre todos los sitios. Un administrador de Microsoft 365 debe aprobar el consentimiento y conceder acceso al sitio.

Crea un archivo `.env` en la carpeta `prototipo_empresa`, copiando `.env.example`.
El archivo `.env` está excluido de Git: no lo compartas ni publiques.

```text
SHAREPOINT_TENANT_ID=<tenant-id>
SHAREPOINT_CLIENT_ID=<application-client-id>
SHAREPOINT_CLIENT_SECRET=<client-secret-value>
SHAREPOINT_SITE_ID=<sharepoint-site-id>
SHAREPOINT_DOCUMENT_LIBRARY=Documentos
SHAREPOINT_FOLDER=Gastos/PorValidar
```

También puedes definir `SHAREPOINT_DRIVE_ID` si ya conoces el identificador de la
biblioteca; en ese caso `SHAREPOINT_SITE_ID` y
`SHAREPOINT_DOCUMENT_LIBRARY` no son necesarios. De lo contrario, la app busca
la biblioteca por nombre en el sitio configurado. El nombre debe coincidir con
el que aparece en SharePoint (por ejemplo, `Documentos` o `Documents`).
`SHAREPOINT_FOLDER` es relativo a la raíz de esa biblioteca; crea previamente
esa carpeta en SharePoint. Los nombres repetidos se guardan con nombre
alternativo para no sobrescribir archivos. El límite de carga de 10 MB está por
debajo del límite del endpoint de carga simple de Graph.

Para obtener estas credenciales, un administrador de Microsoft 365 debe
registrar una aplicación en Microsoft Entra ID, crear un secreto de cliente,
conceder consentimiento al permiso de aplicación `Sites.Selected` de Microsoft
Graph y otorgar a la aplicación permiso de escritura en el sitio de SharePoint
específico. Guarda el **valor** del secreto de cliente, no su ID, en `.env`. No
compartas el secreto en el chat.

`SHAREPOINT_TENANT_ID` y `SHAREPOINT_CLIENT_ID` se encuentran en la página
**Información general** de la aplicación registrada en Entra. Para el
`SHAREPOINT_SITE_ID`, usa el ID del sitio de SharePoint donde se guardarán los
archivos. Si no eres administrador del tenant, solicita a tu equipo de TI que
registre la aplicación, apruebe los permisos y te proporcione esos valores.

Cuando termines de editar `.env`, reinicia Django; la app lo carga al iniciar.
Sin esas credenciales y permisos, la validación local seguirá funcionando pero
no se subirán archivos. Para producción, guarda secretos en un gestor de
secretos en vez de un archivo local.

Alternativamente, en PowerShell puedes definir variables de entorno antes de
iniciar Django:

```powershell
$env:SHAREPOINT_TENANT_ID = "..."
$env:SHAREPOINT_CLIENT_ID = "..."
$env:SHAREPOINT_CLIENT_SECRET = "..."
$env:SHAREPOINT_SITE_ID = "..."
$env:SHAREPOINT_DOCUMENT_LIBRARY = "Documentos"
$env:SHAREPOINT_FOLDER = "Gastos/PorValidar"
python manage.py runserver
```

La identidad de aplicación permite que la aplicación cargue archivos sin iniciar sesión como cada usuario. Antes de producción añade autenticación/autorización a la aplicación, audita quién carga cada documento, define retención y valida permisos, consentimiento y política de datos de tu organización.

## Siguiente etapa: Active Directory

El inicio de sesión local aún no está conectado al dominio. Para integrar AD, despliega primero en Linux/Docker con conectividad segura al controlador de dominio y configura `django-auth-ldap` sobre LDAPS, grupos y permisos mínimos. No expongas LDAP sin TLS ni guardes credenciales del directorio en el repositorio.

Antes de producción: configura `DJANGO_SECRET_KEY`, desactiva `DJANGO_DEBUG`, integra un proveedor real de identidad (por ejemplo, Active Directory mediante LDAPS), agrega autorización por empresa, límites de carga, política de retención/borrado, auditoría y pruebas con datos ficticios. El login mock se desactiva con `DJANGO_DEBUG=false`. La caché de reportes de esta demo es local y efímera.

## Flujo completo local: celular -> computadora -> Hacienda (simulada)

1. **Celular**: abre `/facturas/movil/` (PWA instalable, también empaquetable como app
   Android/iOS en `mobile/`). Captura la factura; se guarda en el dispositivo (IndexedDB) y se
   sincroniza con Django al haber conexión (`client_id` evita duplicados).
2. **Computadora**: abre `/facturas/emitidas/`, pulsa **Marcar lista** y **Enviar a Hacienda**.
3. Django asigna consecutivo y **clave numérica de 50 dígitos** (506 + ddmmaa + cédula(12) +
   consecutivo(20) + situación(1) + código seguridad(8)), genera el XML y llama al **gateway
   Node.js** (`gateway/`), que usa el contrato de la API de recepción de Hacienda: token OAuth2,
   `POST /recepcion` (202) y `GET /recepcion/{clave}` (`ind-estado`: recibido, procesando,
   aceptado, rechazado). Django guarda el estado final (`queued -> processing -> accepted/rejected`).

Arranque: `.\run_local.ps1` (o `-Lan` para usar el teléfono en la misma red con
`DJANGO_ALLOWED_HOSTS=<IP>`). Pruebas: `python manage.py test` y `cd gateway; npm test`.

### Alcance y límites (importante)
- Hacienda se **simula** en `gateway/src/mock-hacienda.js` (`/mock/*`); para forzar un rechazo
  incluye la palabra `RECHAZAR` en la descripción de una línea.
- El XML sigue la estructura de la v4.4 pero **no está firmado (XAdES-EPES) ni usa CABYS reales**;
  por eso `HACIENDA_MODE` distinto de `local` está bloqueado. Para producción hacen falta el
  certificado .p12, firma, validación XSD, CABYS, actividad económica y el IDP real
  (`api-sandbox.comprobanteselectronicos.go.cr`). Verifica siempre la documentación vigente.
- Rutas del gateway: `GET /health`, `POST /auth/token`, `GET /invoices`,
  `POST /invoices/<id>/submit`.
- Apps nativas: ver `mobile/README.md`.
## IA local (Ollama) en el flujo
- **Captura móvil**: botón *Costo y revisión con IA* (`POST /api/v1/ai/review-draft/`). El costo
  (subtotal + IVA = total) lo calcula Django; Ollama sugiere mejoras a descripciones, cantidades y
  tarifas a confirmar.
- **Respuesta de Hacienda**: tras *Enviar a Hacienda* se analiza el resultado (aceptada/rechazada)
  con `POST /api/v1/invoices/<id>/ai-analysis/`; el texto se guarda en `ai_analysis`. Si Ollama no
  responde, se muestra un análisis básico sin IA.
- Solo se usa Ollama en `localhost` (`OLLAMA_MODEL`, por defecto `qwen2.5:3b`). La elección de
  estilos y la corrección de tablas ya existentes siguen en `/diseno/` y `/facturas/`.

## Desarrollo en VS Code
Abre la carpeta del proyecto. `Ctrl+Shift+B`/*Run Task*: **Iniciar todo (Django + Gateway)**,
**Pruebas Django**, **Pruebas gateway**, **Ollama: verificar modelo**. En *Run and Debug* usa el
compuesto **Django + Gateway** para depurar ambos. Primera vez: `pip install -r requirements.txt`,
`python manage.py migrate` y `ollama pull qwen2.5:3b`.

## GuanaBill: tres vías de ingreso (sandbox local)

| Vía | Dónde |
|---|---|
| API REST de proveedores | `/integraciones/` (crear API keys, ejemplos curl/Python/Node). Usa `Authorization: Bearer gb_...` o `X-API-Key`. |
| Excel / CSV | `/facturas/` |
| Factura a factura | `/facturas/nueva/` (asistente paso a paso que llama a `POST /api/v1/invoices/`) |

Datos de prueba locales: `python manage.py seed_guanabill` crea el usuario `proveedor_prueba`
(`Proveedor123!`), una API key y 3 facturas de ejemplo. Solo funciona con `HACIENDA_MODE=local`.
Las API keys se guardan hasheadas (SHA-256) y se muestran una sola vez.


## Flujo de la aplicación

- **Panel** (/panel/): resumen del periodo.
- **Facturas** (/facturas/emitidas/): administración, envío, anulación y corrección.
- **Nueva factura** (/facturas/nueva/) o **Importar Excel/CSV** (/facturas/importar/, plantilla incluida) o la API REST (/integraciones/).
- **Cierres** (/cierres/): validador por periodo (AAAA-MM); bloquea si hay borradores, rechazadas, en cola o totales inconsistentes.
- **Bitácora** (/bitacora/): facturas y cierres aceptados o rechazados, con exportación CSV.


## Estructura del repositorio

```
config/      Proyecto Django (settings, urls)
validador/   App principal: modelos, API REST (api.py), periodos (periods.py), importación, plantillas
gateway/     Gateway Node.js + simulador de Hacienda (rutas ATV: /recepcion/v1/recepcion)
mobile/      Envoltorio Capacitor (Android/iOS)
samples/     Archivos de ejemplo (CSV/XLSX)
scripts/     Utilidades (generar Excel de ejemplo)
```

Datos de prueba: `python manage.py seed_guanabill --user demo` y `python manage.py seed_bancos --user demo --send`.
Pruebas: `python manage.py test` y `cd gateway; npm test`.

## Contribuir

Ramas de trabajo parten de `dev`; los PR se abren hacia `dev` y `main` se actualiza solo desde `dev`.
Antes de abrir un PR ejecuta las dos suites de pruebas. Nunca subas `.env`, `db.sqlite3` ni certificados.
