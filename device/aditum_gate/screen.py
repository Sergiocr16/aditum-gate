"""Cliente de la pantalla pedestal (web/server.js en el puerto 3000).

La pantalla es informativa: nunca debe bloquear ni retrasar el acceso.
Timeout corto, sin reintentos, y si no hay pantalla configurada todos los
metodos son no-op.
"""
import logging
import threading
import time

import requests

from .health import list_input_devices, readers_status
from .settings import SCREEN_BASE_URL

log = logging.getLogger("aditum.screen")

SCREEN_TIMEOUT = 1

# Cadencia del heartbeat de lectores hacia la pantalla; la pantalla marca
# el lector como inactivo si deja de recibirlo (proceso device caido)
READER_HEARTBEAT_SECONDS = 15


class ScreenClient:
    def __init__(self, settings):
        self.enabled = settings.has_screen
        self.place_name = settings.place_name

    def _post(self, endpoint, data=None):
        if not self.enabled:
            return
        try:
            requests.post(f"{SCREEN_BASE_URL}/api/{endpoint}",
                          json=data or {}, timeout=SCREEN_TIMEOUT)
        except requests.exceptions.RequestException as e:
            log.warning("Pantalla inaccesible (%s): %s", endpoint, e)

    def loading(self):
        self._post("loading", {"name": "loading"})

    def accepted(self, name="", door_type="", door_id=""):
        self._post("code-accepted", {"name": name, "doorType": door_type, "doorId": door_id,
                                     "placeName": self.place_name})

    def denied(self, door_type="", door_id=""):
        self._post("code-denied", {"doorType": door_type, "doorId": door_id,
                                   "placeName": self.place_name})

    def wait_for_response(self, name=""):
        self._post("wait-for-response", {"name": name, "placeName": self.place_name})

    def success_exit(self):
        self._post("success-exit")

    def reader_status(self, ok):
        self._post("reader-status", {"ok": bool(ok)})

    def reload(self):
        # El proceso arranco (POST /restart del admin, config nueva): la
        # pantalla recarga para tomar build y config frescos
        self._post("reload")


class ReaderHeartbeat(threading.Thread):
    """Postea periodicamente a la pantalla si los lectores configurados
    estan conectados (mismo criterio que la vista de Salud: presencia del
    hardware en /proc o /dev). La pantalla lo usa para el indicador
    'Lector QR activo' del pie; sin heartbeat reciente se asume inactivo.
    """

    def __init__(self, settings, screen):
        super().__init__(name="reader-heartbeat", daemon=True)
        self.settings = settings
        self.screen = screen

    def run(self):
        while True:
            try:
                readers = readers_status(self.settings, list_input_devices())
                # connected=None (hikvision/none) no cuenta como fallo
                ok = all(r.get("connected") is not False for r in readers)
                self.screen.reader_status(ok)
            except Exception as e:
                log.warning("Heartbeat de lectores fallo: %s", e)
            time.sleep(READER_HEARTBEAT_SECONDS)
