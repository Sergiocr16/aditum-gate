"""Cliente del backend de Aditum: verificacion de codigos QR.

Dos estilos de endpoint conviven en produccion:
  secure -> /aditum-gate-verifier-{entry|exit}-secure/{payload}/{doorId}
            (QR con prefijo "ADTG", token rolling firmado)
  legacy -> /aditum-gate-verifier-{entry|exit}/{payload}/{doorId}
            (QR "ADITUMGATE=<data>"; un exit sin marca EXIT anexa "ENTRY"
            al payload, regla heredada del comportamiento original)
"""
import logging

from . import httpclient

log = logging.getLogger("aditum.backend")

VERIFY_TIMEOUT = (3, 15)


class AditumBackend:
    def __init__(self, settings):
        self.base_url = settings.api_base_url
        self.style = settings.verifier_style

    def build_verify_url(self, door_type, payload, door_id):
        if door_type not in ("entry", "exit"):
            raise ValueError(f"doorType invalido: {door_type}")
        if self.style == "secure":
            return f"{self.base_url}/aditum-gate-verifier-{door_type}-secure/{payload}/{door_id}"
        if door_type == "exit" and "EXIT" not in payload:
            payload = payload + "ENTRY"
        return f"{self.base_url}/aditum-gate-verifier-{door_type}/{payload}/{door_id}"

    def verify(self, door_type, payload, door_id):
        """Devuelve True si el backend autorizo el acceso (HTTP 2xx)."""
        url = self.build_verify_url(door_type, payload, door_id)
        try:
            resp = httpclient.request("GET", url, timeout=VERIFY_TIMEOUT)
            log.info("Verificacion %s puerta %s -> %s", door_type, door_id, resp.status_code)
            return resp.ok
        except Exception as e:
            log.error("Error verificando contra el backend: %s", e)
            return False
