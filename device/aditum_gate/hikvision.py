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
-> 30 min sin acceso al terminal). Ademas la IP queda en cooldown
(AUTH_COOLDOWN_SECONDS) para que los reintentos del backend no sigan
sumando intentos fallidos.

Rendimiento (el telefono espera la respuesta de /update-card):
  - Los terminales de un mismo payload se procesan en paralelo (hasta
    MAX_PARALLEL_TERMINALS hilos); el orden de results es el del payload.
  - Las llamadas ISAPI van por httpclient.isapi_request (sesion sin
    reintentos de urllib3).
  - El HTTPDigestAuth se crea UNA vez por terminal y sync: requests guarda
    el ultimo nonce (thread-local) y manda Authorization preventivo en la
    siguiente llamada, asi cada ISAPI cuesta una sola ida y vuelta en vez
    de 401 + reenvio.

CardStore persiste los employees registrados a disco para que la limpieza
nocturna sobreviva reinicios del proceso (antes vivian en memoria y el cron
de reinicio cada 10 minutos dejaba tarjetas huerfanas en los terminales).
"""
import functools
import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from requests.auth import HTTPDigestAuth

from . import httpclient
from .settings import CARD_STORE_FILE

log = logging.getLogger("aditum.hikvision")

ISAPI_TIMEOUT = (3, 5)
# Borrar un lote de usuarios con sus tarjetas tarda mas que una ISAPI normal (medido:
# un lote de 50 no respondia en 5 s). Lotes chicos y lectura larga.
DELETE_TIMEOUT = (3, 30)
# Vaciado completo (UserInfoDetail/Delete mode=all): asincrono en el terminal; se
# consulta DeleteProcess hasta este tope.
CLEAR_ALL_WAIT_SECONDS = 120
CLEAR_ALL_POLL_SECONDS = 1.0

# Tope de tarjetas vivas por persona en un terminal (modo ventanas): se
# conservan las primeras de cardNos (vigente + siguientes) y se poda el resto.
# Es tambien el limite del firmware (DS-K1T323 V4.23.41): la tarjeta 6 de una
# persona devuelve 400 deviceCardFull.
MAX_CARDS_PER_EMPLOYEE = 5
# Paginas de CardInfo/Search (10 por pagina) que se recorren como maximo
SEARCH_PAGE_SIZE = 10
MAX_SEARCH_PAGES = 5
AUTH_ERROR_STATUSES = (401, 403)
# Terminales de un mismo /update-card que se sincronizan a la vez
MAX_PARALLEL_TERMINALS = 4
# Tras un 401/403 no se vuelve a tocar esa IP durante este lapso: ~7 fallos
# de login seguidos bloquean el terminal 30 min y el backend viejo reintenta
# cada ventana de 22 s, asi que sin cooldown el Pi solo se bloquea mas rapido.
AUTH_COOLDOWN_SECONDS = 300
# Nombre con el que Aditum crea TODOS sus usuarios en el terminal: es la marca que
# distingue lo nuestro (invitaciones, residentes) de los usuarios de planta que el
# administrador carga a mano con nombre real. La limpieza solo toca los nuestros.
ADITUM_USER_NAME = "Bienvenido"
USER_PAGE_SIZE = 30
MAX_USER_PAGES = 1000          # 30 000 usuarios: muy por encima de cualquier terminal
DELETE_BATCH_SIZE = 20
# Si un alta responde "lleno", se barre el terminal en el acto y se reintenta; no mas
# de una vez por terminal en este lapso (cada barrido corta las tarjetas vigentes
# hasta la siguiente rotacion de cada pase).
EMERGENCY_PURGE_INTERVAL_SECONDS = 600
# Credenciales distintas que la limpieza prueba por terminal antes de rendirse.
MAX_CLEANUP_CREDENTIALS = 3


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

    def __init__(self):
        self._tl = threading.local()

    @staticmethod
    def _auth(user, password):
        return HTTPDigestAuth(user, password)

    def _status_of(self, step, ip, resp):
        """Status HTTP de una respuesta ISAPI. En un rechazo (no 2xx) deja en el log el
        cuerpo del terminal (statusString / subStatusCode / errorMsg), que es lo unico
        que dice POR QUE rechazo: sin eso un 400 en ensure_user es indistinguible de
        un employeeNo invalido, un usuario duplicado o un terminal lleno. El cuerpo
        de error del Hikvision nunca trae credenciales. Queda ademas en un
        thread-local para que sync_cards lo anexe como `detail` del error."""
        status = resp.status_code
        if not _ok(status):
            try:
                body = (resp.text or "")[:300].replace("\n", " ")
            except Exception:
                body = "?"
            log.warning("Hikvision %s %s: HTTP %s %s", ip, step, status, body)
            self._tl.last_body = body
        return status

    def _take_last_body(self):
        body = getattr(self._tl, "last_body", None)
        self._tl.last_body = None
        return body if body and body not in ("{}", "?") else None

    def _auth_or(self, auth, user, password):
        """Reutiliza el HTTPDigestAuth recibido o crea uno (compatibilidad con
        cleanup_all y las llamadas sueltas)."""
        return auth if auth is not None else self._auth(user, password)

    def delete_card(self, ip, user, password, employee_no, auth=None):
        """Borra TODAS las tarjetas del employee (modo legacy)."""
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Delete?format=json"
            payload = {"CardInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
            resp = httpclient.isapi_request("PUT", url, json=payload,
                                            auth=auth, timeout=ISAPI_TIMEOUT)
            return self._status_of("delete_card", ip, resp)
        except Exception as e:
            log.error("Error borrando tarjeta en %s: %s", ip, e)
            return None

    def delete_cards(self, ip, user, password, card_nos, auth=None):
        """Borra tarjetas puntuales por numero (una sola llamada)."""
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Delete?format=json"
            payload = {"CardInfoDelCond": {"CardNoList": [{"cardNo": c} for c in card_nos]}}
            resp = httpclient.isapi_request("PUT", url, json=payload,
                                            auth=auth, timeout=ISAPI_TIMEOUT)
            return self._status_of("delete_cards", ip, resp)
        except Exception as e:
            log.error("Error borrando tarjetas en %s: %s", ip, e)
            return None

    def user_exists(self, ip, user, password, employee_no, auth=None):
        """(status del UserInfo/Search, existe). status None = sin respuesta."""
        auth = self._auth_or(auth, user, password)
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
            resp = httpclient.isapi_request("POST", search_url, json=search_payload,
                                            auth=auth, timeout=ISAPI_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                return 200, data.get("UserInfoSearch", {}).get("totalMatches", 0) > 0
            return self._status_of("user_search", ip, resp), False
        except Exception as e:
            log.error("Error buscando usuario en %s: %s", ip, e)
            return None, False

    def create_user(self, ip, user, password, employee_no, auth=None):
        auth = self._auth_or(auth, user, password)
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
            resp = httpclient.isapi_request("POST", url, json=payload,
                                            auth=auth, timeout=ISAPI_TIMEOUT)
            return self._status_of("create_user", ip, resp)
        except Exception as e:
            log.error("Error creando usuario en %s: %s", ip, e)
            return None

    def ensure_user(self, ip, user, password, employee_no, auth=None):
        """Legacy: busca el usuario y, si no esta, lo crea. Sin respuesta en la
        busqueda -> None, sin intentar crear. Un 401/403 en la busqueda se
        devuelve tal cual: intentar crear con las mismas credenciales solo
        sumaria otro login fallido al bloqueo del terminal."""
        auth = self._auth_or(auth, user, password)
        status, exists = self.user_exists(ip, user, password, employee_no, auth=auth)
        if exists:
            return 200
        if status is None or _is_auth_error(status):
            return status
        return self.create_user(ip, user, password, employee_no, auth=auth)

    def register_card(self, ip, user, password, card_no, employee_no, auth=None):
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Record?format=json"
            payload = {
                "CardInfo": {
                    "employeeNo": employee_no,
                    "cardNo": card_no,
                    "cardType": "normalCard",
                }
            }
            resp = httpclient.isapi_request("POST", url, json=payload,
                                            auth=auth, timeout=ISAPI_TIMEOUT)
            return self._status_of("register_card", ip, resp)
        except Exception as e:
            log.error("Error registrando tarjeta en %s: %s", ip, e)
            return None

    def search_cards(self, ip, user, password, employee_no, auth=None):
        """(status, [cardNo, ...]) de las tarjetas del employee en el terminal.

        La lista es None si el terminal no respondio 200 (estado desconocido:
        el caller no debe borrar nada). Recorre paginas mientras el terminal
        responda "MORE".
        """
        auth = self._auth_or(auth, user, password)
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
                resp = httpclient.isapi_request("POST", url, json=payload,
                                                auth=auth, timeout=ISAPI_TIMEOUT)
                if resp.status_code != 200:
                    return self._status_of("search_cards", ip, resp), None
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

    def list_aditum_users(self, ip, user, password, auth=None):
        """(status, [employeeNo, ...]) de los usuarios creados por Aditum en el terminal
        (name == ADITUM_USER_NAME y employeeNo numerico). La lista es None si no se
        pudo recorrer completo (status != 200 o sin respuesta): el caller no borra
        a ciegas. Un solo digest para todas las paginas."""
        auth = self._auth_or(auth, user, password)
        url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Search?format=json"
        found = []
        position = 0
        renewed_at = None
        try:
            for _ in range(MAX_USER_PAGES):
                payload = {"UserInfoSearchCond": {
                    "searchID": "aditum-cleanup",
                    "searchResultPosition": position,
                    "maxResults": USER_PAGE_SIZE,
                }}
                resp = httpclient.isapi_request("POST", url, json=payload,
                                                auth=auth, timeout=ISAPI_TIMEOUT)
                if resp.status_code == 401 and position > 0 and renewed_at != position:
                    # El digest ya autentico (hubo paginas anteriores): el terminal
                    # vence el nonce cada ~8 usos (medido: posiciones 240 y 450 con
                    # paginas de 30). Se renueva la autenticacion y se repite esta
                    # pagina; a lo sumo una renovacion por pagina, asi un 401 real
                    # (credenciales) corta en el segundo intento.
                    log.info("Hikvision %s user_list: nonce vencido en la posicion %s; se renueva el digest",
                             ip, position)
                    auth = self._auth(user, password)
                    renewed_at = position
                    continue
                if resp.status_code != 200:
                    log.warning("Hikvision %s user_list: corte en la posicion %s", ip, position)
                    return self._status_of("user_list", ip, resp), None
                data = resp.json().get("UserInfoSearch", {}) or {}
                page = data.get("UserInfo", []) or []
                for entry in page:
                    employee_no = str(entry.get("employeeNo", ""))
                    if entry.get("name") == ADITUM_USER_NAME and employee_no.isdigit():
                        found.append(employee_no)
                position += len(page)
                if not page or data.get("responseStatusStrg") != "MORE":
                    return 200, found
            log.warning("Hikvision %s: listado de usuarios cortado en %s paginas", ip, MAX_USER_PAGES)
            return 200, found
        except Exception as e:
            log.error("Error listando usuarios en %s: %s", ip, e)
            return None, None

    def count_users(self, ip, user, password, auth=None):
        """(status, cantidad de usuarios en el terminal); cantidad None si no respondio 200.
        Es ademas la sonda de credenciales de la limpieza: un 401 aca cuesta UN login
        fallido y corta antes de intentar el vaciado."""
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Count?format=json"
            resp = httpclient.isapi_request("GET", url, auth=auth, timeout=ISAPI_TIMEOUT)
            if resp.status_code != 200:
                return self._status_of("user_count", ip, resp), None
            return 200, int(resp.json().get("UserInfoCount", {}).get("userNumber", 0))
        except Exception as e:
            log.error("Error contando usuarios en %s: %s", ip, e)
            return None, None

    def delete_all_users(self, ip, user, password, auth=None):
        """Vaciado completo del terminal (UserInfoDetail/Delete mode=all): TODOS los
        usuarios con sus tarjetas, huellas y caras, tambien los cargados a mano.
        Es asincrono en el equipo: se espera DeleteProcess hasta CLEAR_ALL_WAIT_SECONDS.
        Devuelve (status, done): status 200 + done True = vaciado; 400 = el firmware
        no lo soporta (el caller cae al barrido selectivo)."""
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/UserInfoDetail/Delete?format=json"
            resp = httpclient.isapi_request("PUT", url, json={"UserInfoDetail": {"mode": "all"}},
                                            auth=auth, timeout=DELETE_TIMEOUT)
            status = self._status_of("delete_all", ip, resp)
            if not _ok(status):
                return status, False
            process_url = f"http://{ip}/ISAPI/AccessControl/UserInfoDetail/DeleteProcess?format=json"
            deadline = time.monotonic() + CLEAR_ALL_WAIT_SECONDS
            while time.monotonic() < deadline:
                # Digest nuevo en cada consulta: el terminal vence el nonce cada ~8 usos.
                probe = httpclient.isapi_request("GET", process_url, auth=self._auth(user, password),
                                                 timeout=ISAPI_TIMEOUT)
                if probe.status_code != 200:
                    self._status_of("delete_process", ip, probe)
                    return probe.status_code, False
                state = str(probe.json().get("UserInfoDetailDeleteProcess", {}).get("status", "")).lower()
                if state == "success":
                    return 200, True
                if state == "failed":
                    log.warning("Hikvision %s: el vaciado completo termino en failed", ip)
                    return 200, False
                time.sleep(CLEAR_ALL_POLL_SECONDS)
            log.warning("Hikvision %s: el vaciado completo no termino en %ss", ip, CLEAR_ALL_WAIT_SECONDS)
            return 200, False
        except Exception as e:
            log.error("Error vaciando usuarios en %s: %s", ip, e)
            return None, False

    def delete_users(self, ip, user, password, employee_nos, auth=None):
        """Borra varios employees (y sus tarjetas) en una sola llamada."""
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Delete?format=json"
            payload = {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": e} for e in employee_nos]}}
            resp = httpclient.isapi_request("PUT", url, json=payload,
                                            auth=auth, timeout=DELETE_TIMEOUT)
            return self._status_of("delete_users", ip, resp)
        except Exception as e:
            log.error("Error borrando usuarios en %s: %s", ip, e)
            return None

    def delete_user(self, ip, user, password, employee_no, auth=None):
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Delete?format=json"
            payload = {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
            resp = httpclient.isapi_request("PUT", url, json=payload,
                                            auth=auth, timeout=ISAPI_TIMEOUT)
            return self._status_of("delete_user", ip, resp)
        except Exception as e:
            log.error("Error borrando usuario en %s: %s", ip, e)
            return None

    def update_card(self, ip, user, password, card_no, employee_no, auth=None):
        """Legacy: reemplazo total (borrar todas las del employee y registrar una).

        Un 401/403 o un terminal sin respuesta cortan en el primer paso, como
        en sync_cards: con el digest reutilizado cada llamada posterior con
        credenciales malas costaria DOS logins fallidos (preventivo + reenvio)
        y el terminal se bloquea a los ~7.
        """
        auth = self._auth_or(auth, user, password)
        status = self.ensure_user(ip, user, password, employee_no, auth=auth)
        if status is None or _is_auth_error(status):
            return status
        status = self.delete_card(ip, user, password, employee_no, auth=auth)
        if status is None or _is_auth_error(status):
            return status
        return self.register_card(ip, user, password, card_no, employee_no, auth=auth)

    def sync_cards(self, ip, user, password, card_nos, employee_no):
        """Deja vivas en el terminal exactamente las tarjetas de card_nos.

        Orden: asegurar usuario -> consultar tarjetas actuales -> registrar
        las que faltan -> borrar SOLO las que sobran (una llamada). Si las
        que faltan no caben bajo MAX_CARDS_PER_EMPLOYEE, las sobrantes se
        podan antes de registrar (el terminal responde 400 deviceCardFull
        con la persona llena). Con mas de MAX_CARDS_PER_EMPLOYEE en card_nos
        se conservan las primeras (vigente + siguientes). Un 401/403 o un
        terminal sin respuesta cortan el proceso sin reintentar.

        Devuelve {"status", "registered", "deleted", "errors", "elapsedMs"};
        status es 200 sin errores, o el status del primer paso que fallo.
        elapsedMs es el tiempo total contra este terminal (en todos los
        caminos de salida, tambien los cortes tempranos).
        """
        started = time.perf_counter()
        wanted = normalize_card_nos(card_nos)[:MAX_CARDS_PER_EMPLOYEE]
        result = {"status": 200, "registered": [], "deleted": [], "errors": [], "elapsedMs": 0}
        # Un solo digest para todo el sync: tras el primer 401 requests ya
        # manda Authorization preventivo (1 ida y vuelta por ISAPI).
        auth = self._auth(user, password)

        def fail(step, status, **extra):
            detail = self._take_last_body()
            if detail:
                extra["detail"] = detail  # cuerpo del rechazo (subStatusCode), nunca credenciales
            result["errors"].append({"step": step, "status": status, **extra})
            if result["status"] == 200:
                result["status"] = status

        def done():
            result["elapsedMs"] = int((time.perf_counter() - started) * 1000)
            return result

        # 1. Usuario
        status, exists = self.user_exists(ip, user, password, employee_no, auth=auth)
        if status is None or _is_auth_error(status):
            fail("ensure_user", status)
            return done()
        if not exists:
            status = self.create_user(ip, user, password, employee_no, auth=auth)
            if not _ok(status):
                fail("ensure_user", status)
                return done()

        # 2. Tarjetas actuales del employee
        status, current = self.search_cards(ip, user, password, employee_no, auth=auth)
        if status is None or _is_auth_error(status):
            fail("search_cards", status)
            return done()
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
            status = self.delete_cards(ip, user, password, surplus, auth=auth)
            if _ok(status):
                result["deleted"] = surplus
            else:
                fail("delete_cards", status, cardNos=surplus)
                if status is None or _is_auth_error(status):
                    return done()

        # 4. Registrar las que faltan
        for card in missing:
            status = self.register_card(ip, user, password, card, employee_no, auth=auth)
            if _ok(status):
                result["registered"].append(card)
                continue
            fail("register_card", status, cardNo=card)
            if status is None or _is_auth_error(status):
                return done()

        # 5. Borrar las sobrantes, si no hubo que podarlas para hacer espacio
        if pending_surplus:
            status = self.delete_cards(ip, user, password, pending_surplus, auth=auth)
            if _ok(status):
                result["deleted"] = pending_surplus
            else:
                fail("delete_cards", status, cardNos=pending_surplus)
        return done()


TERMINALS_KEY = "__terminals__"


class CardStore:
    """Registro persistente de employees activos por terminal: {ip: {employeeNo: {user, password}}}.

    Bajo TERMINALS_KEY guarda ademas las credenciales VIGENTES de cada terminal (las del
    ultimo sync que autentico): la limpieza nocturna barre con esas, no con las que
    cada entrada traia el dia que se creo (si el administrador cambio la contrasena
    del lector, esas quedaron viejas y la limpieza respondia 401 para siempre).
    """

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
        entry = {"user": user, "password": password}
        with self._lock:
            if self._data.get(ip, {}).get(employee_no) == entry:
                return  # sin cambios: no reescribir el JSON en cada ventana (desgaste de la SD)
            self._data.setdefault(ip, {})[employee_no] = entry
            self._save()

    def remember_terminal(self, ip, user, password):
        entry = {"user": user, "password": password}
        with self._lock:
            terminals = self._data.setdefault(TERMINALS_KEY, {})
            if terminals.get(ip) == entry:
                return
            terminals[ip] = entry
            self._save()

    def terminals(self):
        """{ip: {user, password}} vigentes."""
        with self._lock:
            return json.loads(json.dumps(self._data.get(TERMINALS_KEY, {})))

    def snapshot(self):
        with self._lock:
            return {ip: json.loads(json.dumps(e)) for ip, e in self._data.items() if ip != TERMINALS_KEY}

    def clear(self):
        with self._lock:
            self._data = {}
            self._save()

    def replace(self, data):
        """Deja el registro de employees en data (una sola escritura); conserva las credenciales."""
        with self._lock:
            terminals = self._data.get(TERMINALS_KEY)
            self._data = dict(data)
            if terminals:
                self._data[TERMINALS_KEY] = terminals
            self._save()


class HikvisionService:
    def __init__(self, settings, store=None):
        self.settings = settings
        self.client = HikvisionClient()
        self.store = store if store is not None else CardStore()
        self._auth_blocked = {}  # (ip, user, hash(password)) -> until (time.monotonic())
        self._auth_lock = threading.Lock()
        self._sync_locks = {}  # (ip, employee_no) -> Lock: un sync a la vez por par
        self._purged_at = {}   # ip -> time.monotonic() de la ultima purga de emergencia
        self._cleanup_locks = {}  # ip -> Lock: un barrido a la vez por terminal
        self._cleanup_state_lock = threading.Lock()
        self._cleanup_thread = None
        self.last_cleanup = None  # {"startedAt", "finishedAt", "summary", "results"}

    # ---- cooldown de autenticacion por IP + credencial ----

    @staticmethod
    def _auth_key(ip, user, password):
        """La clave incluye las credenciales: si el backend corrige la
        contrasena del terminal, el siguiente /update-card la prueba de
        inmediato en vez de esperar los 300 s. La contrasena va hasheada para
        que la clave nunca termine en un log."""
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]
        return (ip, user, digest)

    def _auth_cooldown_remaining(self, key):
        """Segundos que faltan para volver a intentar con esa clave (0 = libre)."""
        with self._auth_lock:
            until = self._auth_blocked.get(key)
            if until is None:
                return 0
            remaining = until - time.monotonic()
            if remaining <= 0:
                del self._auth_blocked[key]
                return 0
            return remaining

    def _block_auth(self, key):
        with self._auth_lock:
            self._auth_blocked[key] = time.monotonic() + AUTH_COOLDOWN_SECONDS
        log.warning("Hikvision %s: login rechazado, sin intentos por %ss",
                    key[0], AUTH_COOLDOWN_SECONDS)

    def _sync_lock_for(self, ip, employee_no):
        with self._auth_lock:
            return self._sync_locks.setdefault((ip, employee_no), threading.Lock())

    def _cooldown_result(self, ip, employee_no, remaining, card_nos):
        retry_after = max(1, int(math.ceil(remaining)))
        log.warning("Hikvision %s employee %s: en cooldown de autenticacion, %ss restantes",
                    ip, employee_no, retry_after)
        result = {"ip": ip, "status": 401, "employeeNo": employee_no, "elapsedMs": 0}
        if card_nos is not None:
            result.update({"registered": [], "deleted": [],
                           "errors": [{"step": "auth_cooldown", "status": 401,
                                       "retryAfterSeconds": retry_after}]})
        return result

    # ---- sincronizacion ----

    def _sync_terminal(self, terminal, card_no, employee_no, card_nos):
        """Procesa UN terminal (corre en un hilo del executor). None si no tiene ip.

        Un mismo (ip, employeeNo) se sincroniza de a uno: dos /update-card
        solapados (Flask es threaded y el backend reintenta tras su timeout)
        verian las mismas tarjetas faltantes y el segundo cobraria 400 por
        duplicado. Una excepcion inesperada (p. ej. disco lleno al guardar el
        store) no tumba los results de los demas terminales: ese terminal
        responde status null con step "internal".
        """
        ip = terminal.get("ip")
        if not ip:
            return None
        user = terminal.get("user", "admin")
        password = terminal.get("password", "")
        started = time.perf_counter()
        try:
            with self._sync_lock_for(ip, employee_no):
                return self._run_terminal(ip, user, password, card_no, employee_no, card_nos)
        except Exception:
            log.exception("Hikvision %s employee %s: error inesperado en el sync", ip, employee_no)
            result = {"ip": ip, "status": None, "employeeNo": employee_no,
                      "elapsedMs": int((time.perf_counter() - started) * 1000)}
            if card_nos is not None:
                result.update({"registered": [], "deleted": [],
                               "errors": [{"step": "internal", "status": None}]})
            return result

    def _run_terminal(self, ip, user, password, card_no, employee_no, card_nos):
        key = self._auth_key(ip, user, password)
        remaining = self._auth_cooldown_remaining(key)
        if remaining > 0:
            return self._cooldown_result(ip, employee_no, remaining, card_nos)

        if card_nos is None:
            started = time.perf_counter()
            status = self.client.update_card(ip, user, password, card_no, employee_no)
            result = {"ip": ip, "status": status, "employeeNo": employee_no,
                      "elapsedMs": int((time.perf_counter() - started) * 1000)}
        else:
            sync = self.client.sync_cards(ip, user, password, card_nos, employee_no)
            status = sync["status"]
            result = {"ip": ip, "status": status, "employeeNo": employee_no,
                      "registered": sync["registered"], "deleted": sync["deleted"],
                      "errors": sync["errors"], "elapsedMs": sync["elapsedMs"]}
            if sync["errors"]:
                log.warning("Hikvision %s employee %s: %s", ip, employee_no, sync["errors"])

            if self._looks_full(sync) and self._may_purge(ip):
                # Terminal lleno (cuota de usuarios o tarjetas): sin esto el
                # condominio queda sin QR hasta la limpieza nocturna. Se barren
                # los usuarios de Aditum y se reintenta UNA vez.
                log.warning("Hikvision %s: terminal lleno (%s); purga de emergencia",
                            ip, sync["errors"][0].get("detail"))
                self.cleanup_terminal(ip, user, password)
                sync = self.client.sync_cards(ip, user, password, card_nos, employee_no)
                status = sync["status"]
                result = {"ip": ip, "status": status, "employeeNo": employee_no,
                          "registered": sync["registered"], "deleted": sync["deleted"],
                          "errors": sync["errors"], "elapsedMs": sync["elapsedMs"],
                          "purged": True}

        if _is_auth_error(status):
            # Login rechazado: el usuario seguro NO quedo creado. Sin store la
            # limpieza nocturna no insiste contra el terminal sumando intentos
            # fallidos al bloqueo por login ilegal (asi era antes: se guardaba
            # siempre).
            self._block_auth(key)
        else:
            # Cualquier otro fallo (timeout, 5xx) pudo ocurrir DESPUES de crear
            # el usuario: se guarda igual para que la limpieza nocturna no deje
            # employees huerfanos en el terminal.
            self.store.add(ip, employee_no, user, password)
            if status is not None:
                # El terminal autentico con estas credenciales: son las vigentes.
                self.store.remember_terminal(ip, user, password)
        return result

    @staticmethod
    def _looks_full(sync):
        errors = sync.get("errors") or []
        if not errors or errors[0].get("step") != "ensure_user" or errors[0].get("status") != 400:
            return False
        return "full" in str(errors[0].get("detail", "")).lower()

    def _may_purge(self, ip):
        now = time.monotonic()
        with self._auth_lock:
            last = self._purged_at.get(ip)
            if last is not None and now - last < EMERGENCY_PURGE_INTERVAL_SECONDS:
                return False
            self._purged_at[ip] = now
            return True

    def update_card(self, card_no, employee_no, terminals, card_nos=None):
        """card_nos None -> legacy (reemplazo); lista -> sincronizacion por ventanas.

        Los terminales se procesan en paralelo (hasta MAX_PARALLEL_TERMINALS);
        results conserva el orden del payload y omite los que no tienen ip.
        """
        started = time.perf_counter()
        targets = list(terminals)
        if not targets:
            return []
        worker = functools.partial(self._sync_terminal, card_no=card_no,
                                   employee_no=employee_no, card_nos=card_nos)
        workers = min(MAX_PARALLEL_TERMINALS, len(targets))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hikvision-sync") as executor:
            results = [r for r in executor.map(worker, targets) if r is not None]
        total_ms = int((time.perf_counter() - started) * 1000)
        log.info("update-card employee=%s terminals=%d elapsed=%dms",
                 employee_no, len(results), total_ms)
        return results

    def cleanup_terminal(self, ip, user, password):
        """Barre UN terminal: lista todos sus usuarios y borra en lote los de Aditum
        (name == ADITUM_USER_NAME), esten o no en el store. Devuelve
        {"status", "deleted": [...], "failed": [...] | None}; failed None = no se
        pudo listar (nada se borro). Un 401/403 corta en el acto: insistir solo
        suma intentos al bloqueo por login ilegal del terminal."""
        with self._cleanup_state_lock:
            lock = self._cleanup_locks.setdefault(ip, threading.Lock())
        # Un barrido a la vez por terminal: la limpieza manual y las purgas de
        # emergencia de cada pase abierto se pisaban listando el mismo lector.
        with lock:
            return self._sweep_terminal(ip, user, password)

    def _sweep_terminal(self, ip, user, password):
        """Vaciado completo del terminal (mode=all); si ese firmware no lo soporta
        (400), barrido selectivo por lotes de los usuarios de Aditum."""
        cstatus, before = self.client.count_users(ip, user, password)
        if cstatus is None or _is_auth_error(cstatus):
            return {"ip": ip, "status": cstatus, "deleted": 0, "failed": None, "mode": "all"}
        status, done = self.client.delete_all_users(ip, user, password)
        if done:
            _, after = self.client.count_users(ip, user, password)
            deleted = max(0, (before or 0) - (after or 0)) if before is not None else None
            log.info("Limpieza Hikvision %s: vaciado completo, %s usuarios antes, %s despues",
                     ip, before, after)
            return {"ip": ip, "status": 200, "deleted": deleted, "failed": 0, "mode": "all"}
        if status is None or _is_auth_error(status):
            return {"ip": ip, "status": status, "deleted": 0, "failed": None, "mode": "all"}
        log.info("Hikvision %s: vaciado completo no disponible (HTTP %s); barrido selectivo", ip, status)
        auth = self.client._auth(user, password)
        status, found = self.client.list_aditum_users(ip, user, password, auth=auth)
        if found is None:
            return {"ip": ip, "status": status, "deleted": 0, "failed": None, "mode": "sweep"}
        deleted, failed = [], []
        for i in range(0, len(found), DELETE_BATCH_SIZE):
            batch = found[i:i + DELETE_BATCH_SIZE]
            # Digest nuevo por lote: el terminal vence el nonce cada ~8 usos y un lote
            # rechazado por eso se contaria como fallo. Cuesta una ida y vuelta extra
            # por lote de 50 usuarios: irrelevante.
            st = self.client.delete_users(ip, user, password, batch)
            if _ok(st):
                deleted.extend(batch)
                continue
            failed.extend(batch)
            if st is None or _is_auth_error(st):
                failed.extend(found[i + DELETE_BATCH_SIZE:])
                status = st
                break
        log.info("Limpieza Hikvision %s: %s usuarios de Aditum borrados, %s fallidos",
                 ip, len(deleted), len(failed))
        return {"ip": ip, "status": status, "deleted": len(deleted), "failed": len(failed), "mode": "sweep"}

    @staticmethod
    def _credential_candidates(learned, employees):
        """Credenciales a probar contra un terminal, en orden: la aprendida en el
        ultimo sync que autentico y, detras, hasta MAX_CLEANUP_CREDENTIALS
        DISTINTAS del store de la mas nueva a la mas vieja. El store puede mezclar
        la contrasena actual con una cambiada hace meses (una entrada existente
        que vuelve a sincronizar actualiza sus credenciales pero conserva su
        posicion): probar unas pocas distintas cuesta a lo sumo unos 401, nunca
        uno por entrada."""
        candidates = []
        seen = set()
        for creds in ([learned] if learned else []) + list(reversed(list(employees.values()))):
            key = (creds.get("user"), creds.get("password"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(creds)
            if len(candidates) >= MAX_CLEANUP_CREDENTIALS:
                break
        return candidates

    def cleanup_all(self):
        """Limpieza nocturna: vacia cada terminal conocido con sus credenciales
        VIGENTES (UserInfoDetail/Delete mode=all; si el firmware no lo soporta,
        barrido selectivo de los usuarios de Aditum).

        El store es la lista de lo que este Pi registro; lo que quedo de antes no
        esta ahi y era lo que llenaba el terminal hasta que rechazaba altas. Las
        entradas de un terminal que no se pudo limpiar (apagado, 401) se conservan
        para la proxima. Devuelve un resultado por terminal:
        {"ip", "status", "deleted", "failed", "mode"} (failed None = no se pudo).
        """
        snapshot = self.store.snapshot()
        terminals = self.store.terminals()
        results = []
        pending = {}
        for ip in sorted(set(snapshot) | set(terminals)):
            employees = snapshot.get(ip, {})
            candidates = self._credential_candidates(terminals.get(ip), employees)
            if not candidates:
                continue
            swept = None
            for creds in candidates:
                swept = self.cleanup_terminal(ip, creds["user"], creds["password"])
                if swept["failed"] is None and _is_auth_error(swept["status"]):
                    continue  # esa credencial ya no sirve: probar la siguiente distinta
                if swept["failed"] is not None:
                    self.store.remember_terminal(ip, creds["user"], creds["password"])
                break
            results.append(swept)
            if swept["failed"] is None and employees:
                pending[ip] = employees
        self.store.replace(pending)
        kept = sum(len(e) for e in pending.values())
        log.info("Limpieza Hikvision: %s terminales limpiados, %s con fallo, %s entradas pendientes para la proxima",
                 sum(1 for r in results if r["failed"] is not None), sum(1 for r in results if r["failed"] is None), kept)
        return results

    # ---- limpieza en segundo plano (boton del panel) ----

    @staticmethod
    def summarize_cleanup(results):
        terminals = [{"ip": r["ip"], "status": r["status"], "mode": r.get("mode"),
                      "deleted": r.get("deleted") or 0,
                      "failed": (r.get("failed") if r.get("failed") is not None else 1)} for r in results]
        return {
            "deleted": sum(t["deleted"] for t in terminals),
            "failed": sum(t["failed"] for t in terminals),
            "terminals": terminals,
        }

    def start_cleanup_async(self):
        """Lanza cleanup_all en un hilo. False si ya hay uno corriendo. El panel
        consulta cleanup_status(): un barrido de miles de usuarios tarda mas que
        el timeout del proxy y que la paciencia del healthcheck."""
        with self._cleanup_state_lock:
            if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
                return False
            self.last_cleanup = {"startedAt": time.time(), "finishedAt": None, "summary": None}
            self._cleanup_thread = threading.Thread(target=self._run_cleanup_async,
                                                    name="hikvision-cleanup-manual", daemon=True)
            self._cleanup_thread.start()
            return True

    def _run_cleanup_async(self):
        started = time.perf_counter()
        try:
            results = self.cleanup_all()
            summary = self.summarize_cleanup(results)
        except Exception as e:
            log.exception("Fallo la limpieza manual")
            summary = {"deleted": 0, "failed": 0, "terminals": [], "error": str(e)}
        summary["elapsedMs"] = int((time.perf_counter() - started) * 1000)
        log.warning("Limpieza Hikvision manual: %s", summary["terminals"] or summary.get("error"))
        with self._cleanup_state_lock:
            self.last_cleanup = dict(self.last_cleanup or {}, finishedAt=time.time(), summary=summary)

    def cleanup_status(self):
        with self._cleanup_state_lock:
            running = self._cleanup_thread is not None and self._cleanup_thread.is_alive()
            return {"running": running, "last": self.last_cleanup}

    def wait_cleanup(self, timeout=30):
        thread = self._cleanup_thread
        if thread is not None:
            thread.join(timeout)

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
