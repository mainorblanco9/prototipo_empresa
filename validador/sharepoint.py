import json
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import msal
from requests.exceptions import RequestException
from django.conf import settings

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
HTTP_TIMEOUT_SECONDS = 30


class SharePointError(Exception):
    pass


class SharePointNotConfigured(SharePointError):
    pass


def is_sharepoint_configured():
    has_application_credentials = all(
        (
            settings.SHAREPOINT_TENANT_ID,
            settings.SHAREPOINT_CLIENT_ID,
            settings.SHAREPOINT_CLIENT_SECRET,
        )
    )
    return has_application_credentials and bool(
        settings.SHAREPOINT_DRIVE_ID
        or (
            settings.SHAREPOINT_SITE_ID
            and settings.SHAREPOINT_DOCUMENT_LIBRARY
        )
    )


def _configured_drive_id(token):
    if settings.SHAREPOINT_DRIVE_ID:
        return settings.SHAREPOINT_DRIVE_ID
    if not settings.SHAREPOINT_SITE_ID:
        raise SharePointNotConfigured(
            "Configura SHAREPOINT_SITE_ID o SHAREPOINT_DRIVE_ID en .env."
        )

    site_id = quote(settings.SHAREPOINT_SITE_ID, safe=",")
    drives = _graph_request(
        f"{GRAPH_BASE_URL}/sites/{site_id}/drives",
        token,
        method="GET",
    )
    configured_drive_name = settings.SHAREPOINT_DOCUMENT_LIBRARY.strip()
    available_drives = drives.get("value", [])
    if configured_drive_name:
        matching_drives = [
            drive
            for drive in available_drives
            if drive.get("name", "").casefold() == configured_drive_name.casefold()
        ]
        if not matching_drives:
            available_names = ", ".join(
                drive.get("name", "")
                for drive in available_drives
                if drive.get("name")
            )
            raise SharePointError(
                f"No se encontró la biblioteca '{configured_drive_name}' en el sitio. "
                f"Bibliotecas disponibles: {available_names or 'ninguna'}."
            )
        return matching_drives[0]["id"]
    if len(available_drives) == 1:
        return available_drives[0]["id"]
    raise SharePointError(
        "El sitio tiene varias bibliotecas de documentos. Configura "
        "SHAREPOINT_DOCUMENT_LIBRARY con el nombre exacto de la biblioteca."
    )


def _safe_filename(filename):
    filename = filename.replace("\\", "/").split("/")[-1].strip()
    filename = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", filename)
    if filename in ("", ".", ".."):
        raise SharePointError("El archivo no tiene un nombre válido para SharePoint.")
    return filename


def _destination_path(filename):
    folder = settings.SHAREPOINT_FOLDER
    segments = [segment for segment in folder.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise SharePointError("La carpeta configurada para SharePoint no es válida.")
    return "/".join([*segments, _safe_filename(filename)])


def _graph_request(url, token, method, body=None, content_type="application/json"):
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
    )
    request.headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            graph_message = payload.get("error", {}).get("message")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            graph_message = None
        detail = f": {graph_message}" if graph_message else ""
        raise SharePointError(
            f"Microsoft Graph respondió HTTP {exc.code}{detail}"
        ) from exc
    except URLError as exc:
        raise SharePointError(
            f"No se pudo conectar con Microsoft Graph: {exc.reason}"
        ) from exc
    except (OSError, HTTPException, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SharePointError(
            f"Respuesta inválida o tiempo de espera agotado en Microsoft Graph: {type(exc).__name__}"
        ) from exc


def upload_to_sharepoint(upload, filename):
    if not is_sharepoint_configured():
        raise SharePointNotConfigured(
            "SharePoint no está configurado. Completa las credenciales de Entra ID "
            "y define SHAREPOINT_DRIVE_ID, o bien SHAREPOINT_SITE_ID junto con "
            "SHAREPOINT_DOCUMENT_LIBRARY."
        )

    try:
        application = msal.ConfidentialClientApplication(
            settings.SHAREPOINT_CLIENT_ID,
            authority=(
                "https://login.microsoftonline.com/"
                f"{settings.SHAREPOINT_TENANT_ID}"
            ),
            client_credential=settings.SHAREPOINT_CLIENT_SECRET,
        )
        token_result = application.acquire_token_for_client(scopes=GRAPH_SCOPE)
    except (ValueError, OSError, RequestException) as exc:
        raise SharePointError(
            f"No se pudo iniciar la autenticación de Microsoft Graph: {type(exc).__name__}"
        ) from exc

    access_token = token_result.get("access_token")
    if not access_token:
        error_code = token_result.get("error", "unknown_error")
        raise SharePointError(
            f"Microsoft Entra ID no emitió un token ({error_code}). Revisa la app y sus permisos."
        )

    drive_id = _configured_drive_id(access_token)
    destination = _destination_path(filename)
    encoded_path = "/".join(quote(part, safe="") for part in destination.split("/"))
    url = (
        f"{GRAPH_BASE_URL}/drives/{quote(drive_id, safe='')}"
        f"/root:/{encoded_path}:/content?@microsoft.graph.conflictBehavior=rename"
    )

    try:
        upload.seek(0)
        content = upload.read()
    except (OSError, ValueError) as exc:
        raise SharePointError("No se pudo leer de nuevo el archivo validado.") from exc

    file_record = _graph_request(
        url,
        access_token,
        method="PUT",
        body=content,
        content_type="application/octet-stream",
    )
    return {
        "name": file_record.get("name", filename),
        "web_url": file_record.get("webUrl", ""),
        "item_id": file_record.get("id", ""),
    }
