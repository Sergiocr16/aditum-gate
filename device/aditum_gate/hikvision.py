"""Integracion con terminales Hikvision (ISAPI).

El Pi actua de puente: Aditum hace POST /update-card con el token QR rolling
y aqui se registra como tarjeta en los terminales. Las credenciales de cada
terminal llegan en el payload (el backend las tiene en la tabla gate); NO se
guardan en la configuracion.

Dos modos de registro conviven:
  - legacy (payload con solo cardNo): reemplazo total — se borran las
    tarjetas del employee y se registra la nueva. Sin cambios.
  - ventanas (payload con cardNos): el backend manda el conjunto COMPLETO de
    tarjetas que deben quedar vivas (vigente primero, luego las ventanas
    siguientes). El Pi consulta las que el terminal ya tiene, registra las
    que faltan y borra SOLO las que sobran, siempre despues de registrar,
    asi el visitante nunca queda sin tarjeta valida durante el refresco. La
    excepcion es el tope de tarjetas por persona del terminal: si no hay
    espacio se podan las vencidas primero (ver sync_cards).

Un 401/403 del terminal corta el proceso sin reintentar: cada intento
fallido cuenta para el bloqueo por login ilegal del Hikvision (~7 intentos
-> 30 min sin acceso al terminal).

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

# Tope de tarjetas vivas por persona en un terminal (modo ventanas): se
# conservan las primeras de cardNos (vigente + siguientes) y se poda el resto.
# Es tambien el limite del firmware (DS-K1T323 V4.23.41): la tarjeta 6 de una
# persona devuelve 400 deviceCardFull.
MAX_CARDS_PER_EMPLOYEE = 5
# Paginas de CardInfo/Search (10 por pagina) que se recorren como maximo
SEARCH_PAGE_SIZE = 10
MAX_SEARCH_PAGES = 5
AUTH_ERROR_STATUSES = (401, 403)


def _is_auth_error(status):
    return status in AUTH_ERROR_STATUSES


def _ok(status):
    return status is not None and 200 <= status < 300


def normalize_card_nos(card_nos):
    """Lista de cardNo como strings no vacios, sin repetidos, en orden."""
    seen = set()
    result = []
    for card in card_nos or []:
        card = str(card).strip()
        if card and card not in seen:
            seen.add(card)
            result.append(card)
    return result


class HikvisionClient:
    """Operaciones ISAPI contra un terminal."""

    @staticmethod
    def _auth(user, password):
        return HTTPDigestAuth(user, password)

    def delete_card(self, ip, user, password, employee_no):
        """Borra TODAS las tarjetas del employee (modo legacy)."""
        try:
            url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Delete?format=json"
            payload = {"CardInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
            resp = httpclient.request("PUT", url, json=payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            return resp.status_code
        except Exception as e:
            log.error("Error borrando tarjeta en %s: %s", ip, e)
            return None

    def delete_cards(self, ip, user, password, card_nos):
        """Borra tarjetas puntuales por numero (una sola llamada)."""
        try:
            url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Delete?format=json"
            payload = {"CardInfoDelCond": {"CardNoList": [{"cardNo": c} for c in card_nos]}}
            resp = httpclient.request("PUT", url, json=payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            return resp.status_code
        except Exception as e:
            log.error("Error borrando tarjetas en %s: %s", ip, e)
            return None

    def user_exists(self, ip, user, password, employee_no):
        """(status del UserInfo/Search, existe). status None = sin respuesta."""
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
                return 200, data.get("UserInfoSearch", {}).get("totalMatches", 0) > 0
            return resp.status_code, False
        except Exception as e:
            log.error("Error buscando usuario en %s: %s", ip, e)
            return None, False

    def create_user(self, ip, user, password, employee_no):
        try:
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
            log.error("Error creando usuario en %s: %s", ip, e)
            return None

    def ensure_user(self, ip, user, password, employee_no):
        """Legacy: busca el usuario y, si no esta, lo crea. Mismo comportamiento
        de siempre (sin respuesta en la busqueda -> None, sin intentar crear)."""
        status, exists = self.user_exists(ip, user, password, employee_no)
        if exists:
            return 200
        if status is None:
            return None
        return self.create_user(ip, user, password, employee_no)

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

    def search_cards(self, ip, user, password, employee_no):
        """(status, [cardNo, ...]) de las tarjetas del employee en el terminal.

        La lista es None si el terminal no respondio 200 (estado desconocido:
        el caller no debe borrar nada). Recorre paginas mientras el terminal
        responda "MORE".
        """
        url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Search?format=json"
        cards = []
        position = 0
        try:
            for _ in range(MAX_SEARCH_PAGES):
                payload = {
                    "CardInfoSearchCond": {
                        "searchID": "1",
                        "maxResults": SEARCH_PAGE_SIZE,
                        "searchResultPosition": position,
                        "EmployeeNoList": [{"employeeNo": employee_no}],
                    }
                }
                resp = httpclient.request("POST", url, json=payload,
                                          auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
                if resp.status_code != 200:
                    return resp.status_code, None
                data = resp.json().get("CardInfoSearch", {})
                page = [c.get("cardNo") for c in data.get("CardInfo", []) if c.get("cardNo")]
                cards.extend(page)
                if data.get("responseStatusStrg") != "MORE" or not page:
                    break
                position += len(page)
            return 200, cards
        except Exception as e:
            log.error("Error consultando tarjetas en %s: %s", ip, e)
            return None, None

    def delete_user(self, ip, user, password, employee_no):
        try:
            url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Delete?format=json"
            payload = {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
            resp = httpclient.request("PUT", url, json=payload,
                                      auth=self._auth(user, password), timeout=ISAPI_TIMEOUT)
            return resp.status_code
        except Exception as e:
            log.error("Error borrando usuario en %s: %s", ip, e)
            return None

    def update_card(self, ip, user, password, card_no, employee_no):
        """Legacy: reemplazo total (borrar todas las del employee y registrar una)."""
        self.ensure_user(ip, user, password, employee_no)
        self.delete_card(ip, user, password, employee_no)
        return self.register_card(ip, user, password, card_no, employee_no)

    def sync_cards(self, ip, user, password, card_nos, employee_no):
        """Deja vivas en el terminal exactamente las tarjetas de card_nos.

        Orden: asegurar usuario -> consultar tarjetas actuales -> registrar
        las que faltan -> borrar SOLO las que sobran (una llamada). Si las
        que faltan no caben bajo MAX_CARDS_PER_EMPLOYEE, las sobrantes se
        podan antes de registrar (el terminal responde 400 deviceCardFull
        con la persona llena). Con mas de MAX_CARDS_PER_EMPLOYEE en card_nos
        se conservan las primeras (vigente + siguientes). Un 401/403 o un
        terminal sin respuesta cortan el proceso sin reintentar.

        Devuelve {"status", "registered", "deleted", "errors"}; status es 200
        sin errores, o el status del primer paso que fallo.
        """
        wanted = normalize_card_nos(card_nos)[:MAX_CARDS_PER_EMPLOYEE]
        result = {"status": 200, "registered": [], "deleted": [], "errors": []}

        def fail(step, status, **extra):
            result["errors"].append({"step": step, "status": status, **extra})
            if result["status"] == 200:
                result["status"] = status

        # 1. Usuario
        status, exists = self.user_exists(ip, user, password, employee_no)
        if status is None or _is_auth_error(status):
            fail("ensure_user", status)
            return result
        if not exists:
            status = self.create_user(ip, user, password, employee_no)
            if not _ok(status):
                fail("ensure_user", status)
                return result

        # 2. Tarjetas actuales del employee
        status, current = self.search_cards(ip, user, password, employee_no)
        if status is None or _is_auth_error(status):
            fail("search_cards", status)
            return result
        known_state = current is not None
        if not known_state:
            # Estado desconocido: registrar todo lo pedido y no borrar nada
            fail("search_cards", status)
            current = []

        missing = [c for c in wanted if c not in current]
        surplus = [c for c in current if c not in wanted] if known_state else []
        pending_surplus = surplus

        # 3. Podar ANTES solo si no hay espacio: el terminal topa en
        # MAX_CARDS_PER_EMPLOYEE tarjetas por persona (400 deviceCardFull) y
        # registrar primero fallaria. No rompe la invariante: una sobrante no
        # esta en card_nos (ya vencio); la vigente esta en ambos conjuntos y
        # nunca se toca.
        if surplus and len(current) + len(missing) > MAX_CARDS_PER_EMPLOYEE:
            pending_surplus = []
            status = self.delete_cards(ip, user, password, surplus)
            if _ok(status):
                result["deleted"] = surplus
            else:
                fail("delete_cards", status, cardNos=surplus)
                if status is None or _is_auth_error(status):
                    return result

        # 4. Registrar las que faltan
        for card in missing:
            status = self.register_card(ip, user, password, card, employee_no)
            if _ok(status):
                result["registered"].append(card)
                continue
            fail("register_card", status, cardNo=card)
            if status is None or _is_auth_error(status):
                return result

        # 5. Borrar las sobrantes, si no hubo que podarlas para hacer espacio
        if pending_surplus:
            status = self.delete_cards(ip, user, password, pending_surplus)
            if _ok(status):
                result["deleted"] = pending_surplus
            else:
                fail("delete_cards", status, cardNos=pending_surplus)
        return result


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

    def replace(self, data):
        """Deja el registro en data (una sola escritura)."""
        with self._lock:
            self._data = data
            self._save()


class HikvisionService:
    def __init__(self, settings, store=None):
        self.settings = settings
        self.client = HikvisionClient()
        self.store = store if store is not None else CardStore()

    def update_card(self, card_no, employee_no, terminals, card_nos=None):
        """card_nos None -> legacy (reemplazo); lista -> sincronizacion por ventanas."""
        results = []
        for terminal in terminals:
            ip = terminal.get("ip")
            user = terminal.get("user", "admin")
            password = terminal.get("password", "")
            if not ip:
                continue
            if card_nos is None:
                status = self.client.update_card(ip, user, password, card_no, employee_no)
                results.append({"ip": ip, "status": status, "employeeNo": employee_no})
            else:
                sync = self.client.sync_cards(ip, user, password, card_nos, employee_no)
                results.append({"ip": ip, "status": sync["status"], "employeeNo": employee_no,
                                "registered": sync["registered"], "deleted": sync["deleted"],
                                "errors": sync["errors"]})
                if sync["errors"]:
                    log.warning("Hikvision %s employee %s: %s", ip, employee_no, sync["errors"])
            self.store.add(ip, employee_no, user, password)
        return results

    def cleanup_all(self):
        """Borra los employees registrados y olvida SOLO los que se borraron.

        Un terminal caido a las 2 AM devuelve None: si igual se olvidara la
        entrada, ese usuario quedaria en el terminal sin nadie que lo
        recuerde (huerfano invisible para la limpieza siguiente). Borrar un
        employee que ya no existe devuelve 200, asi que reintentar es barato
        y las entradas no se acumulan solas.
        """
        results = []
        pending = {}
        for ip, employees in self.store.snapshot().items():
            for employee_no, creds in employees.items():
                status = self.client.delete_user(ip, creds["user"], creds["password"], employee_no)
                results.append({"ip": ip, "employeeNo": employee_no, "status": status})
                if not _ok(status):
                    pending.setdefault(ip, {})[employee_no] = creds
        self.store.replace(pending)
        failed = sum(len(e) for e in pending.values())
        log.info("Limpieza Hikvision: %s usuarios borrados, %s pendientes para la proxima",
                 len(results) - failed, failed)
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
