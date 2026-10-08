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
            return resp.status_code
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
            return resp.status_code
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
            return resp.status_code, False
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
            return resp.status_code
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
            return resp.status_code
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

    def delete_user(self, ip, user, password, employee_no, auth=None):
        auth = self._auth_or(auth, user, password)
        try:
            url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Delete?format=json"
            payload = {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
            resp = httpclient.isapi_request("PUT", url, json=payload,
                                            auth=auth, timeout=ISAPI_TIMEOUT)
            return resp.status_code
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
        entry = {"user": user, "password": password}
        with self._lock:
            if self._data.get(ip, {}).get(employee_no) == entry:
                return  # sin cambios: no reescribir el JSON en cada ventana (desgaste de la SD)
            self._data.setdefault(ip, {})[employee_no] = entry
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
        self._auth_blocked = {}  # (ip, user, hash(password)) -> until (time.monotonic())
        self._auth_lock = threading.Lock()
        self._sync_locks = {}  # (ip, employee_no) -> Lock: un sync a la vez por par

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
        return result

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
