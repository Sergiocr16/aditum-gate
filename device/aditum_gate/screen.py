"""Cliente de la pantalla pedestal (web/server.js en el puerto 3000).

La pantalla es informativa: nunca debe bloquear ni retrasar el acceso.
Timeout corto, sin reintentos, y si no hay pantalla configurada todos los
metodos son no-op.
"""
import logging

import requests

from .settings import SCREEN_BASE_URL

log = logging.getLogger("aditum.screen")

SCREEN_TIMEOUT = 1


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
