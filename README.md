# Prototipo empresarial: validación de gastos

Aplicación inicial en Django para cargar CSV/XLSX, validar datos de gastos y descargar un reporte. La explicación opcional de OpenAI recibe únicamente conteos y categorías de errores; no se envían filas, nombres de proveedores ni montos al modelo.

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
conecta al servicio local en `http://localhost:11434`; no necesita una API key y
las solicitudes del asesor de diseño y del resumen de validación se procesan en
tu equipo. La descarga del modelo requiere Internet y espacio en disco. El
rendimiento depende de la memoria y el procesador disponibles.

Abre <http://127.0.0.1:8000/> e inicia sesión con el usuario mock `demo` y la contraseña `Demo123!`. La cuenta mock solo se habilita con `DJANGO_DEBUG=true` (valor local predeterminado) y no debe usarse en producción. Puedes cambiar el usuario y la contraseña de desarrollo con `DEMO_USERNAME` y `DEMO_PASSWORD`.

Después del primer inicio de sesión, el prototipo presenta un selector de estilo (minimalista, abstracto o corporativo). Puedes describir el ambiente visual deseado y pedir una recomendación; la IA selecciona y aplica automáticamente uno de los temas permitidos. La preferencia se guarda por usuario y se puede cambiar luego con “Cambiar diseño”. No se envían datos de archivos a esa recomendación.

Ollama local es el proveedor predeterminado. Configura `OLLAMA_BASE_URL` para cambiar su dirección y `OLLAMA_MODEL` para seleccionar otro modelo ya descargado. Para usar OpenAI en lugar del modelo local, configura `AI_PROVIDER=openai` y `OPENAI_API_KEY` en el entorno. Sin un proveedor de IA disponible, la validación determinista y la selección manual de tema siguen funcionando.

En Git Bash, activa el entorno con:

```bash
source .venv/Scripts/activate
```

## Alcance actual

- Archivos `.csv` UTF-8 y `.xlsx`, máximo 10 MB y 20.000 filas.
- Revisa fecha, proveedor, factura duplicada por proveedor, moneda, importes no negativos y suma subtotal + impuesto.
- Permite descargar un CSV con el resultado; el enlace temporal expira en 10 minutos.
- Sube el archivo original a SharePoint por Microsoft Graph solo si hay al menos una fila y todas las filas pasan las validaciones.
- Si hay errores, configuración incompleta o una falla de Microsoft Graph, muestra el resultado y explica que no hubo una carga exitosa.
- Protege la página de carga y los reportes con una sesión Django; incluye una cuenta mock solo para desarrollo.
- Guarda por usuario la preferencia de tema de la interfaz; la recomendación IA se limita a temas visuales predefinidos y nunca modifica código.
- Permite usar Ollama local para recomendaciones y resúmenes, sin enviar estos prompts a un servicio externo.
- No escribe en un sistema contable ni sustituye revisión financiera/fiscal.
- SQLite es solo para desarrollo local.

## Configurar Microsoft Graph / SharePoint

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
