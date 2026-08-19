"""Autenticacion por token de dispositivo para el API Flask.

Fail-secure: un before_request global exige el token en TODOS los paths;
lo publico es una allowlist explicita (PUBLIC_PATHS). Un endpoint nuevo
queda protegido sin hacer nada.

Modo sin provisionar: si no existe device-token.txt el API queda abierto
(compatibilidad: el backend historico no manda credencial y los portones
deben seguir funcionando durante la migracion de la flota). Se loguea ERROR
con rate-limit y GET /status lo delata con provisioned=false. Al provisionar
el token (PUT /token TOFU o device-token.txt) el enforcement pasa a ser
estricto: 401 sin token valido o sesion admin.
"""
import hmac
import logging
import time

from flask import jsonify, request

from .admin_auth import is_admin_logged_in

log = logging.getLogger("aditum.auth")

# Paths accesibles sin credencial. Agregar uno aqui es una decision de
# seguridad: debe quedar evidente en el code review.
# /admin es solo el HTML estatico del editor local (su contenido se tapa con
# el login); /admin/login y /admin/session son el flujo de autenticacion.
# /anpr-event lo postea la camara Hikvision (no sabe mandar bearer): el
# endpoint filtra por IP de origen (best-effort) y solo ENCOLA lecturas —
# no abre portones ni toca configuracion (ver api.py, TAR-1035).
PUBLIC_PATHS = {"/", "/admin", "/admin/login", "/admin/logout", "/admin/session",
                "/anpr-event"}

UNPROVISIONED_LOG_INTERVAL = 60  # segundos entre logs de "sin provisionar"
_last_unprovisioned_log = 0.0


def _extract_token():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip()
    return request.headers.get("X-Device-Token", "").strip()


def init_auth(app, settings):
    @app.before_request
    def require_device_token():
        global _last_unprovisioned_log

        if request.path in PUBLIC_PATHS:
            return None

        # Una persona logueada en el editor local (cookie de sesion admin) es
        # superusuario del API local, tenga o no token el dispositivo.
        if is_admin_logged_in():
            return None

        # settings.device_token se lee en cada request: PUT /token lo rota
        # en memoria sin reiniciar el proceso.
        expected = settings.device_token

        if not expected:
            now = time.monotonic()
            if now - _last_unprovisioned_log >= UNPROVISIONED_LOG_INTERVAL:
                _last_unprovisioned_log = now
                log.error("Dispositivo SIN PROVISIONAR: API abierto (compatibilidad "
                          "con la flota sin token). Provisionar via PUT /token "
                          "(TOFU) o device-token.txt para activar el enforcement")
            # Sin token el API queda abierto (comportamiento historico): el
            # backend actual no manda credencial y los portones deben seguir
            # funcionando durante la migracion. GET /status delata el estado
            # (provisioned=false). Al provisionar, el enforcement es estricto.
            return None

        provided = _extract_token()
        if provided and hmac.compare_digest(provided, expected):
            return None

        return jsonify({"error": "unauthorized"}), 401
