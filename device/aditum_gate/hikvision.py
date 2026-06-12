"""Integracion con terminales Hikvision (ISAPI).

El Pi actua de puente: Aditum hace POST /update-card con el token QR rolling
(cada ~22s) y aqui se registra como tarjeta en los terminales. Las
credenciales de cada terminal llegan en el payload (el backend las tiene en
la tabla gate); NO se guardan en la configuracion.

CardStore persiste los employees registrados a disco para que la limpieza
nocturna sobreviva reinicios del proceso (antes vivian en memoria y el cron
de reinicio cada 10 minutos dejaba tarjetas huerfanas en los terminales).
"""
import json
import logging
import os
import tempfile
import threading
import time
from datetime import date

from requests.auth import HTTPDigestAuth

from . import httpclient
from .settings import CARD_STORE_FILE

log = logging.getLogger("aditum.hikvision")

ISAPI_TIMEOUT = (3, 5)


class HikvisionClient:
    """Operaciones ISAPI contra un terminal."""

    @staticmethod
    def _auth(user, password):
        return HTTPDigestAuth(user, password)

    def delete_card(self, ip, user, password, employee_no):
        try:
            url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Delete?format=json"
            payload = {"CardInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
            resp = httpclient.request("PUT", url, json=payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            return resp.status_code
        except Exception as e:
            log.error("Error borrando tarjeta en %s: %s", ip, e)
            return None

    def ensure_user(self, ip, user, password, employee_no):
        try:
            search_url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Search?format=json"
            search_payload = {
                "UserInfoSearchCond": {
                    "searchID": "1",
                    "maxResults": 1,
                    "searchResultPosition": 0,
                    "EmployeeNoList": [{"employeeNo": employee_no}],
                }
            }
            resp = httpclient.request("POST", search_url, json=search_payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("UserInfoSearch", {}).get("totalMatches", 0) > 0:
                    return 200

            url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Record?format=json"
            payload = {
                "UserInfo": {
                    "employeeNo": employee_no,
                    "name": "Bienvenido",
                    "userType": "normal",
                    "Valid": {
                        "enable": True,
                        "beginTime": "2024-01-01T00:00:00",
                        "endTime": "2037-12-31T23:59:59",
                        "timeType": "local",
                    },
                    "RightPlan": [{"doorNo": 1, "planTemplateNo": "1"}],
                    "doorRight": "1",
                    "localUIRight": False,
                }
            }
            resp = httpclient.request("POST", url, json=payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            return resp.status_code
        except Exception as e:
            log.error("Error asegurando usuario en %s: %s", ip, e)
            return None

    def register_card(self, ip, user, password, card_no, employee_no):
        try:
            url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Record?format=json"
            payload = {
                "CardInfo": {
                    "employeeNo": employee_no,
                    "cardNo": card_no,
                    "cardType": "normalCard",
                }
            }
            resp = httpclient.request("POST", url, json=payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            return resp.status_code
        except Exception as e:
            log.error("Error registrando tarjeta en %s: %s", ip, e)
            return None

    def delete_user(self, ip, user, password, employee_no):
        try:
            url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Delete?format=json"
            payload = {
                "UserInfoDetail": {
                    "mode": "byEmployeeNo",
                    "EmployeeNoList": [{"employeeNo": employee_no}],
                }
            }
            resp = httpclient.request("PUT", url, json=payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            return resp.status_code
        except Exception as e:
            log.error("Error borrando usuario en %s: %s", ip, e)
            return None

    def update_card(self, ip, user, password, card_no, employee_no):
        self.ensure_user(ip, user, password, employee_no)
        self.delete_card(ip, user, password, employee_no)
        return self.register_card(ip, user, password, card_no, employee_no)


class CardStore:
    """Registro persistente de employees activos por terminal: {ip: {employeeNo: {user, password}}}."""

    def __init__(self, path=CARD_STORE_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            if self.path.exists():
                with open(self.path) as f:
                    return json.load(f)
        except (OSError, ValueError) as e:
            log.error("CardStore corrupto, se reinicia: %s", e)
        return {}

    def _save(self):
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent),
                                        prefix=".cards.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp_path, self.path)
        os.chmod(self.path, 0o600)  # contiene credenciales de terminales

    def add(self, ip, employee_no, user, password):
        with self._lock:
            self._data.setdefault(ip, {})[employee_no] = {"user": user, "password": password}
            self._save()

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._data))

    def clear(self):
        with self._lock:
            self._data = {}
            self._save()


class HikvisionService:
    def __init__(self, settings):
        self.settings = settings
        self.client = HikvisionClient()
        self.store = CardStore()

    def update_card(self, card_no, employee_no, terminals):
        results = []
        for terminal in terminals:
            ip = terminal.get("ip")
            user = terminal.get("user", "admin")
            password = terminal.get("password", "")
            if not ip:
                continue
            status = self.client.update_card(ip, user, password, card_no, employee_no)
            results.append({"ip": ip, "status": status, "employeeNo": employee_no})
            self.store.add(ip, employee_no, user, password)
        return results

    def cleanup_all(self):
        results = []
        for ip, employees in self.store.snapshot().items():
            for employee_no, creds in employees.items():
                status = self.client.delete_user(ip, creds["user"], creds["password"], employee_no)
                results.append({"ip": ip, "employeeNo": employee_no, "status": status})
        self.store.clear()
        log.info("Limpieza Hikvision: %s usuarios borrados", len(results))
        return results

    def start_nightly_cleanup(self):
        thread = threading.Thread(target=self._nightly_loop,
                                  name="hikvision-cleanup", daemon=True)
        thread.start()

    def _nightly_loop(self):
        """Chequea la hora cada minuto en vez de un sleep gigante: sobrevive
        reinicios y cambios de hora del sistema."""
        last_run = None
        while True:
            now = time.localtime()
            today = date.today()
            if now.tm_hour == self.settings.nightly_cleanup_hour and last_run != today:
                try:
                    self.cleanup_all()
                except Exception:
                    log.exception("Fallo la limpieza nocturna")
                last_run = today
            time.sleep(60)
