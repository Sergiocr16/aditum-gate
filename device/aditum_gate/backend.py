"""Cliente del backend de Aditum: verificacion de codigos QR.

Dos formatos de QR conviven en produccion y AMBOS se aceptan siempre, en
cualquier equipo, sin nada que configurar: el prefijo de cada codigo decide
el endpoint que se consulta.
  "ADTG<token>"       -> /aditum-gate-verifier-{entry|exit}-secure/{payload}/{doorId}
                         (token rolling firmado; instalaciones nuevas)
  "ADITUMGATE=<data>" -> /aditum-gate-verifier-{entry|exit}/{payload}/{doorId}
                         (formato legacy; un exit sin marca EXIT anexa "ENTRY"
                         al payload, regla heredada del comportamiento original)
Cualquier otro prefijo se deniega localmente sin consultar al backend.
"""
import logging

from . import httpclient

log = logging.getLogger("aditum.backend")

VERIFY_TIMEOUT = (3, 15)

# (prefijo, estilo de endpoint). La comparacion es case-insensitive y el
# prefijo se recorta antes de mandar el payload. Los dos formatos divergen
# en el tercer caracter (ADT.. vs ADI..), asi que nunca hay ambiguedad.
QR_FORMATS = (
    ("ADITUMGATE=", "legacy"),
    ("ADTG", "secure"),
)
QR_PREFIXES = tuple(prefix for prefix, _ in QR_FORMATS)


def match_qr(text):
    """Devuelve (prefijo, estilo) del formato que casa con el codigo, o None."""
    upper = text.upper()
    for prefix, style in QR_FORMATS:
        if upper.startswith(prefix):
            return prefix, style
    return None


class AditumBackend:
    def __init__(self, settings):
        self.base_url = settings.api_base_url

    def build_verify_url(self, door_type, payload, door_id, style):
        if door_type not in ("entry", "exit"):
            raise ValueError(f"doorType invalido: {door_type}")
        if style == "secure":
            return f"{self.base_url}/aditum-gate-verifier-{door_type}-secure/{payload}/{door_id}"
        if style != "legacy":
            raise ValueError(f"estilo de verificacion invalido: {style}")
        if door_type == "exit" and "EXIT" not in payload:
            payload = payload + "ENTRY"
        return f"{self.base_url}/aditum-gate-verifier-{door_type}/{payload}/{door_id}"

    def verify(self, door_type, payload, door_id, style):
        """Devuelve True si el backend autorizo el acceso (HTTP 2xx)."""
        url = self.build_verify_url(door_type, payload, door_id, style)
        try:
            resp = httpclient.request("GET", url, timeout=VERIFY_TIMEOUT)
            log.info("Verificacion %s (%s) puerta %s -> %s",
                     door_type, style, door_id, resp.status_code)
            return resp.ok
        except Exception as e:
            log.error("Error verificando contra el backend: %s", e)
            return False
