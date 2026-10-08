"""Tests de la sincronizacion de tarjetas Hikvision (modo ventanas y legacy).

Correr desde la raiz del repo (sin red: el ISAPI se simula):
    python3 -m unittest discover -s device/tests -t device -v
"""
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError, ProtocolError

from aditum_gate import admin_auth, hikvision, httpclient
from aditum_gate import api as api_module
from aditum_gate import settings as settings_module
from aditum_gate.hikvision import MAX_CARDS_PER_EMPLOYEE, CardStore, HikvisionService
from aditum_gate.settings import Settings

TERMINAL = {"ip": "10.0.0.5", "user": "admin", "password": "secreto"}
EMP = "12345"

# Los modulos loguean errores esperados (401, terminal apagado): fuera del reporte
logging.disable(logging.CRITICAL)

USER_SEARCH = "AccessControl/UserInfo/Search"
USER_RECORD = "AccessControl/UserInfo/Record"
CARD_SEARCH = "AccessControl/CardInfo/Search"
CARD_RECORD = "AccessControl/CardInfo/Record"
CARD_DELETE = "AccessControl/CardInfo/Delete"
USER_DELETE = "AccessControl/UserInfo/Delete"


def strip_timing(result):
    """Copia del result sin elapsedMs (varia por corrida) para comparar por igualdad."""
    return {k: v for k, v in result.items() if k != "elapsedMs"}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class FakeTerminal:
    """Simula el ISAPI de un DS-K1T323: usuarios y tarjetas por employee."""

    def __init__(self, users=(), cards=(), auth_ok=True, card_search_status=200,
                 card_limit=MAX_CARDS_PER_EMPLOYEE):
        self.users = set(users)
        self.cards = list(cards)  # [(employeeNo, cardNo)] en orden de registro
        self.auth_ok = auth_ok
        self.card_search_status = card_search_status
        self.card_limit = card_limit
        self.calls = []  # (method, path, payload)
        self.auth_objects = set()  # id() de cada HTTPDigestAuth recibido

    def cards_of(self, emp):
        return [c for e, c in self.cards if e == emp]

    def paths(self):
        return [p for _, p, _ in self.calls]

    def request(self, method, url, timeout=None, json=None, auth=None, **kwargs):
        path = url.split("/ISAPI/", 1)[1].split("?")[0]
        self.calls.append((method, path, json))
        self.auth_objects.add(id(auth))
        if not self.auth_ok:
            return FakeResponse(401)
        if path == USER_SEARCH:
            emp = json["UserInfoSearchCond"]["EmployeeNoList"][0]["employeeNo"]
            return FakeResponse(200, {"UserInfoSearch": {"totalMatches": 1 if emp in self.users else 0}})
        if path == USER_RECORD:
            self.users.add(json["UserInfo"]["employeeNo"])
            return FakeResponse(200)
        if path == CARD_SEARCH:
            if self.card_search_status != 200:
                return FakeResponse(self.card_search_status)
            cond = json["CardInfoSearchCond"]
            emp = cond["EmployeeNoList"][0]["employeeNo"]
            mine = self.cards_of(emp)
            pos = cond["searchResultPosition"]
            page = mine[pos:pos + cond["maxResults"]]
            more = pos + len(page) < len(mine)
            return FakeResponse(200, {"CardInfoSearch": {
                "searchID": cond["searchID"],
                "responseStatusStrg": "MORE" if more else ("OK" if page else "NO MATCH"),
                "numOfMatches": len(page), "totalMatches": len(mine),
                "CardInfo": [{"employeeNo": emp, "cardNo": c, "cardType": "normalCard"} for c in page],
            }})
        if path == CARD_RECORD:
            info = json["CardInfo"]
            if len(self.cards_of(info["employeeNo"])) >= self.card_limit:
                return FakeResponse(400, {"statusString": "Invalid Content",
                                          "subStatusCode": "deviceCardFull"})
            self.cards.append((info["employeeNo"], info["cardNo"]))
            return FakeResponse(200)
        if path == CARD_DELETE:
            cond = json["CardInfoDelCond"]
            if "CardNoList" in cond:
                nos = {c["cardNo"] for c in cond["CardNoList"]}
                self.cards = [(e, c) for e, c in self.cards if c not in nos]
            else:
                emps = {e["employeeNo"] for e in cond["EmployeeNoList"]}
                self.cards = [(e, c) for e, c in self.cards if e not in emps]
            return FakeResponse(200)
        if path == USER_DELETE:
            emps = {e["employeeNo"] for e in json["UserInfoDelCond"]["EmployeeNoList"]}
            self.users -= emps
            self.cards = [(e, c) for e, c in self.cards if e not in emps]
            return FakeResponse(200)
        raise AssertionError(f"ISAPI inesperado: {method} {path}")


class HikvisionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CardStore(Path(self.tmp.name) / "cards.json")
        self.service = HikvisionService(mock.Mock(nightly_cleanup_hour=2), store=self.store)

    def attach(self, terminal):
        patcher = mock.patch.object(hikvision.httpclient, "isapi_request", side_effect=terminal.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        return terminal

    def sync(self, card_nos):
        return self.service.update_card(card_no=card_nos[0], employee_no=EMP,
                                        terminals=[TERMINAL], card_nos=card_nos)[0]


class SyncCardsTests(HikvisionBase):
    def test_primera_apertura_registra_tres_y_no_borra(self):
        t = self.attach(FakeTerminal())
        r = self.sync(["A", "B", "C"])
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["registered"], ["A", "B", "C"])
        self.assertEqual(r["deleted"], [])
        self.assertEqual(r["errors"], [])
        self.assertEqual(t.cards_of(EMP), ["A", "B", "C"])
        self.assertNotIn(CARD_DELETE, t.paths())
        self.assertIn(USER_RECORD, t.paths())  # el usuario no existia: se crea
        # El store guarda (ip, employeeNo, credenciales) para la limpieza nocturna
        self.assertEqual(self.store.snapshot(),
                         {TERMINAL["ip"]: {EMP: {"user": "admin", "password": "secreto"}}})

    def test_refresco_registra_una_y_borra_una(self):
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, "A"), (EMP, "B"), (EMP, "C")]))
        r = self.sync(["B", "C", "D"])
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["registered"], ["D"])
        self.assertEqual(r["deleted"], ["A"])
        self.assertEqual(t.cards_of(EMP), ["B", "C", "D"])
        paths = t.paths()
        self.assertEqual(paths.count(CARD_RECORD), 1)  # B y C no se re-registran
        self.assertNotIn(USER_RECORD, paths)  # el usuario ya existia
        deletes = [(m, j) for m, p, j in t.calls if p == CARD_DELETE]
        self.assertEqual(deletes, [("PUT", {"CardInfoDelCond": {"CardNoList": [{"cardNo": "A"}]}})])
        # Nunca borrar antes de registrar
        self.assertLess(paths.index(CARD_RECORD), paths.index(CARD_DELETE))

    def test_mismo_payload_dos_veces_es_idempotente(self):
        t = self.attach(FakeTerminal())
        self.sync(["A", "B", "C"])
        r = self.sync(["A", "B", "C"])
        self.assertEqual((r["registered"], r["deleted"], r["errors"]), ([], [], []))
        self.assertEqual(t.cards_of(EMP), ["A", "B", "C"])

    def test_sin_cardnos_es_legacy_reemplazo_total(self):
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, "A"), (EMP, "B")]))
        r = self.service.update_card(card_no="Z", employee_no=EMP, terminals=[TERMINAL])[0]
        self.assertEqual(strip_timing(r), {"ip": TERMINAL["ip"], "status": 200, "employeeNo": EMP})
        self.assertGreaterEqual(r["elapsedMs"], 0)
        self.assertEqual(t.paths(), [USER_SEARCH, CARD_DELETE, CARD_RECORD])
        self.assertEqual(t.calls[1][2], {"CardInfoDelCond": {"EmployeeNoList": [{"employeeNo": EMP}]}})
        self.assertEqual(t.cards_of(EMP), ["Z"])

    def test_tope_de_cinco_registra_solo_las_primeras(self):
        t = self.attach(FakeTerminal(users=[EMP]))
        r = self.sync([f"C{i}" for i in range(1, 8)])  # 7 pedidas
        self.assertEqual(r["registered"], ["C1", "C2", "C3", "C4", "C5"])
        self.assertEqual(len(t.cards_of(EMP)), MAX_CARDS_PER_EMPLOYEE)
        self.assertNotIn(CARD_DELETE, t.paths())

    def test_poda_todas_las_vencidas_en_una_sola_llamada(self):
        viejas = [f"V{i}" for i in range(12)]  # mas de una pagina de CardInfo/Search
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, v) for v in viejas]))
        r = self.sync(["A", "B", "C"])
        self.assertEqual(r["registered"], ["A", "B", "C"])
        self.assertEqual(r["deleted"], viejas)
        self.assertEqual(t.cards_of(EMP), ["A", "B", "C"])
        self.assertEqual(t.paths().count(CARD_SEARCH), 2)  # paginado (10 por pagina)
        self.assertEqual(t.paths().count(CARD_DELETE), 1)

    def test_refresco_al_tope_poda_antes_para_hacer_espacio(self):
        """Con la persona llena, registrar primero da 400 deviceCardFull: hay que
        podar la vencida antes. La vigente nunca es sobrante, no se toca."""
        vivas = [f"V{i}" for i in range(1, 6)]  # el terminal ya esta al tope
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, v) for v in vivas]))
        r = self.sync(vivas[1:] + ["V6"])  # la ventana avanza uno
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["registered"], ["V6"])
        self.assertEqual(r["deleted"], ["V1"])
        self.assertEqual(t.cards_of(EMP), ["V2", "V3", "V4", "V5", "V6"])
        paths = t.paths()
        self.assertLess(paths.index(CARD_DELETE), paths.index(CARD_RECORD))

    def test_ventana_saltada_al_tope_no_deja_a_la_persona_sin_tarjetas(self):
        """Pi que estuvo caido: llega un conjunto disjunto con la persona llena.
        Sin podar antes, los 5 registros fallan y el borrado la deja en cero."""
        viejas = [f"V{i}" for i in range(1, 6)]
        nuevas = [f"N{i}" for i in range(1, 6)]
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, v) for v in viejas]))
        r = self.sync(nuevas)
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["registered"], nuevas)
        self.assertEqual(r["deleted"], viejas)
        self.assertEqual(t.cards_of(EMP), nuevas)

    def test_sin_espacio_solo_se_poda_lo_vencido(self):
        """La poda anticipada nunca borra una tarjeta que sigue en cardNos."""
        vivas = [f"V{i}" for i in range(1, 6)]
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, v) for v in vivas]))
        self.sync(vivas[2:] + ["V6", "V7"])
        borradas = [j for m, p, j in t.calls if p == CARD_DELETE]
        self.assertEqual(borradas, [{"CardInfoDelCond": {"CardNoList": [
            {"cardNo": "V1"}, {"cardNo": "V2"}]}}])
        self.assertEqual(t.cards_of(EMP), ["V3", "V4", "V5", "V6", "V7"])

    def test_limpieza_nocturna_borra_el_usuario_del_terminal(self):
        """delete_user va con UserInfoDelCond: con UserInfoDetail el firmware
        responde 400 y la limpieza dejaba usuarios huerfanos."""
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, "A")]))
        self.sync(["A", "B"])
        results = self.service.cleanup_all()
        self.assertEqual(results, [{"ip": TERMINAL["ip"], "employeeNo": EMP, "status": 200}])
        self.assertEqual(t.users, set())
        self.assertEqual(t.cards_of(EMP), [])
        self.assertEqual(self.store.snapshot(), {})

    def test_limpieza_conserva_lo_que_no_pudo_borrar(self):
        """Un terminal caido a las 2 AM no se olvida: olvidarlo dejaria a su
        usuario en el terminal e invisible para toda limpieza posterior."""
        vivo = FakeTerminal(users=[EMP], cards=[(EMP, "A")])
        caido = {"ip": "10.0.0.9", "user": "admin", "password": "otra"}
        segundo = FakeTerminal()  # responde durante el sync, se apaga antes de la limpieza
        apagado = {"on": False}

        def router(method, url, **kwargs):
            if caido["ip"] in url:
                if apagado["on"]:
                    raise ConnectionError("terminal apagado")
                return segundo.request(method, url, **kwargs)
            return vivo.request(method, url, **kwargs)

        patcher = mock.patch.object(hikvision.httpclient, "isapi_request", side_effect=router)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service.update_card(card_no="A", employee_no=EMP,
                                 terminals=[TERMINAL, caido], card_nos=["A"])
        self.assertEqual(set(self.store.snapshot()), {TERMINAL["ip"], caido["ip"]})
        apagado["on"] = True
        self.service.cleanup_all()
        self.assertEqual(vivo.users, set())  # el que respondio se borro
        self.assertEqual(self.store.snapshot(),  # el caido queda pendiente
                         {caido["ip"]: {EMP: {"user": "admin", "password": "otra"}}})

    def test_401_no_reintenta(self):
        t = self.attach(FakeTerminal(auth_ok=False))
        r = self.sync(["A", "B", "C"])
        self.assertEqual(t.paths(), [USER_SEARCH])  # un solo intento contra el terminal
        self.assertEqual(r["status"], 401)
        self.assertEqual(r["errors"], [{"step": "ensure_user", "status": 401}])
        self.assertEqual((r["registered"], r["deleted"]), ([], []))

    def test_consulta_fallida_registra_todo_y_no_borra(self):
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, "A")], card_search_status=500))
        r = self.sync(["B", "C"])
        self.assertEqual(r["registered"], ["B", "C"])
        self.assertEqual(r["deleted"], [])
        self.assertEqual(r["errors"], [{"step": "search_cards", "status": 500}])
        self.assertEqual(r["status"], 500)
        self.assertNotIn(CARD_DELETE, t.paths())
        self.assertEqual(t.cards_of(EMP), ["A", "B", "C"])

    def test_terminal_sin_respuesta_corta_sin_insistir(self):
        calls = []

        def down(method, url, **kwargs):
            calls.append(url)
            raise ConnectionError("terminal apagado")

        patcher = mock.patch.object(hikvision.httpclient, "isapi_request", side_effect=down)
        patcher.start()
        self.addCleanup(patcher.stop)
        r = self.sync(["A", "B"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["status"], None)
        self.assertEqual(r["errors"], [{"step": "ensure_user", "status": None}])


class ParallelAndAuthTests(HikvisionBase):
    """Terminales en paralelo, digest reutilizado, cooldown tras 401 y store."""

    def test_dos_terminales_en_paralelo_resultados_en_orden(self):
        """Barrier(2) en la primera llamada de cada terminal: solo pasa si los
        dos hilos corren a la vez (en secuencia el primero haria timeout)."""
        otro = {"ip": "10.0.0.6", "user": "admin", "password": "otra"}
        terminals = {TERMINAL["ip"]: FakeTerminal(), otro["ip"]: FakeTerminal()}
        barrier = threading.Barrier(2, timeout=5)
        first_seen = set()
        lock = threading.Lock()

        def router(method, url, **kwargs):
            ip = url.split("http://", 1)[1].split("/", 1)[0]
            with lock:
                first = ip not in first_seen
                first_seen.add(ip)
            if first:
                barrier.wait()  # BrokenBarrierError si el otro terminal no llega
            return terminals[ip].request(method, url, **kwargs)

        patcher = mock.patch.object(hikvision.httpclient, "isapi_request", side_effect=router)
        patcher.start()
        self.addCleanup(patcher.stop)
        results = self.service.update_card(card_no="A", employee_no=EMP,
                                           terminals=[TERMINAL, otro], card_nos=["A", "B"])
        self.assertEqual([r["ip"] for r in results], [TERMINAL["ip"], otro["ip"]])
        self.assertEqual([r["status"] for r in results], [200, 200])
        for t in terminals.values():
            self.assertEqual(t.cards_of(EMP), ["A", "B"])
        self.assertEqual(set(self.store.snapshot()), {TERMINAL["ip"], otro["ip"]})

    def test_digest_auth_se_crea_una_vez_por_terminal_y_sync(self):
        t = self.attach(FakeTerminal())
        with mock.patch.object(hikvision, "HTTPDigestAuth", wraps=hikvision.HTTPDigestAuth) as digest:
            r = self.sync(["A", "B", "C"])
        self.assertEqual(r["status"], 200)
        # usuario nuevo: UserInfo/Search + Record + CardInfo/Search + 3 Record
        self.assertEqual(len(t.calls), 6)
        self.assertEqual(digest.call_count, 1)
        self.assertEqual(len(t.auth_objects), 1)

    def test_401_activa_cooldown_y_el_segundo_sync_no_toca_el_terminal(self):
        t = self.attach(FakeTerminal(auth_ok=False))
        r1 = self.sync(["A", "B"])
        self.assertEqual(r1["status"], 401)
        self.assertEqual(len(t.calls), 1)
        r2 = self.sync(["A", "B"])
        self.assertEqual(len(t.calls), 1)  # no se volvio a llamar al terminal
        self.assertEqual(r2["status"], 401)
        self.assertEqual((r2["registered"], r2["deleted"]), ([], []))
        self.assertEqual(r2["errors"][0]["step"], "auth_cooldown")
        self.assertEqual(r2["errors"][0]["status"], 401)
        self.assertTrue(1 <= r2["errors"][0]["retryAfterSeconds"] <= hikvision.AUTH_COOLDOWN_SECONDS)
        self.assertEqual(r2["elapsedMs"], 0)

    def test_cooldown_vence_y_se_reintenta(self):
        t = self.attach(FakeTerminal(auth_ok=False))
        clock = {"now": 1000.0}
        with mock.patch.object(hikvision.time, "monotonic", side_effect=lambda: clock["now"]):
            self.sync(["A"])
            self.sync(["A"])
            self.assertEqual(len(t.calls), 1)
            clock["now"] += hikvision.AUTH_COOLDOWN_SECONDS + 1
            r = self.sync(["A"])
        self.assertEqual(len(t.calls), 2)  # vencido el cooldown se vuelve a intentar
        self.assertEqual(r["errors"], [{"step": "ensure_user", "status": 401}])

    def test_cooldown_legacy_responde_401_sin_llamar(self):
        t = self.attach(FakeTerminal(auth_ok=False))
        self.service.update_card(card_no="A", employee_no=EMP, terminals=[TERMINAL])
        calls = len(t.calls)
        r = self.service.update_card(card_no="A", employee_no=EMP, terminals=[TERMINAL])[0]
        self.assertEqual(len(t.calls), calls)
        self.assertEqual(r, {"ip": TERMINAL["ip"], "status": 401, "employeeNo": EMP, "elapsedMs": 0})

    def test_store_no_guarda_si_fallo_el_usuario(self):
        self.attach(FakeTerminal(auth_ok=False))
        self.sync(["A", "B"])
        self.assertEqual(self.store.snapshot(), {})

    def test_store_guarda_si_el_usuario_quedo_aunque_falle_despues(self):
        self.attach(FakeTerminal(users=[EMP], card_search_status=500))
        r = self.sync(["A", "B"])
        self.assertEqual(r["status"], 500)
        self.assertEqual(self.store.snapshot(),
                         {TERMINAL["ip"]: {EMP: {"user": "admin", "password": "secreto"}}})

    def test_elapsed_ms_en_todos_los_caminos_de_salida(self):
        for terminal in (FakeTerminal(), FakeTerminal(auth_ok=False),
                         FakeTerminal(users=[EMP], card_search_status=500)):
            with self.subTest(terminal=terminal):
                self.attach(terminal)
                r = self.sync(["A"])
                self.assertIsInstance(r["elapsedMs"], int)
                self.assertGreaterEqual(r["elapsedMs"], 0)
                self.service._auth_blocked.clear()

    def test_sesion_isapi_sin_reintentos(self):
        adapter = httpclient.get_isapi_session().get_adapter("http://10.0.0.5/")
        self.assertEqual(adapter.max_retries.total, 0)
        # La sesion del backend conserva sus reintentos
        self.assertEqual(httpclient.get_session().get_adapter("https://x/").max_retries.total, 3)

    def test_legacy_corta_en_el_primer_401(self):
        """Con el digest reutilizado, cada llamada posterior con credenciales
        malas costaria DOS logins fallidos; produccion hacia 3 llamadas."""
        t = self.attach(FakeTerminal(auth_ok=False))
        r = self.service.update_card(card_no="A", employee_no=EMP, terminals=[TERMINAL])[0]
        self.assertEqual(r["status"], 401)
        self.assertEqual(t.paths(), [USER_SEARCH])
        self.assertEqual(self.store.snapshot(), {})

    def test_cooldown_no_aplica_con_otras_credenciales(self):
        t = self.attach(FakeTerminal(auth_ok=False))
        self.sync(["A"])
        self.assertEqual(len(t.calls), 1)
        t.auth_ok = True  # el backend corrigio la contrasena del terminal
        corregida = dict(TERMINAL, password="corregida")
        r = self.service.update_card(card_no="A", employee_no=EMP, terminals=[corregida], card_nos=["A"])[0]
        self.assertEqual(r["status"], 200)
        self.assertEqual(len(t.calls), 1 + 4)  # user search + record, card search + record
        # las credenciales viejas siguen en cooldown
        r2 = self.sync(["A"])
        self.assertEqual(r2["errors"][0]["step"], "auth_cooldown")
        self.assertEqual(len(t.calls), 5)

    def test_mismo_employee_y_terminal_se_sincroniza_de_a_uno(self):
        t = FakeTerminal(users=[EMP])

        def lento(method, url, **kwargs):
            resp = t.request(method, url, **kwargs)
            if CARD_SEARCH in url:
                time.sleep(0.05)  # ventana para que el otro hilo vea el mismo estado
            return resp

        patcher = mock.patch.object(hikvision.httpclient, "isapi_request", side_effect=lento)
        patcher.start()
        self.addCleanup(patcher.stop)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.sync(["A", "B"]))) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual([r["status"] for r in results], [200, 200])
        self.assertEqual(t.cards_of(EMP), ["A", "B"])  # sin duplicados
        self.assertEqual(sorted(len(r["registered"]) for r in results), [0, 2])

    def test_store_guarda_si_el_registro_no_respondio_tras_crear_usuario(self):
        t = FakeTerminal()

        def sin_respuesta(method, url, **kwargs):
            if CARD_RECORD in url:
                raise requests.ConnectionError("read timeout")
            return t.request(method, url, **kwargs)

        patcher = mock.patch.object(hikvision.httpclient, "isapi_request", side_effect=sin_respuesta)
        patcher.start()
        self.addCleanup(patcher.stop)
        r = self.sync(["A"])
        self.assertIsNone(r["status"])
        self.assertIn(EMP, t.users)  # el usuario SI quedo creado en el terminal
        esperado = {TERMINAL["ip"]: {EMP: {"user": "admin", "password": "secreto"}}}
        self.assertEqual(self.store.snapshot(), esperado)
        # modo legacy: mismo criterio
        self.store = CardStore(Path(self.tmp.name) / "cards2.json")
        self.service = HikvisionService(mock.Mock(nightly_cleanup_hour=2), store=self.store)
        r = self.service.update_card(card_no="A", employee_no=EMP, terminals=[TERMINAL])[0]
        self.assertIsNone(r["status"])
        self.assertEqual(self.store.snapshot(), esperado)

    def test_excepcion_en_un_terminal_no_tumba_a_los_demas(self):
        otro = {"ip": "10.0.0.6", "user": "admin", "password": "otra"}
        self.attach(FakeTerminal())
        original = self.store.add

        def add(ip, *args):
            if ip == otro["ip"]:
                raise OSError("disco lleno")
            return original(ip, *args)

        with mock.patch.object(self.store, "add", side_effect=add):
            results = self.service.update_card(card_no="A", employee_no=EMP,
                                               terminals=[TERMINAL, otro], card_nos=["A"])
        self.assertEqual([r["ip"] for r in results], [TERMINAL["ip"], otro["ip"]])
        self.assertEqual(results[0]["status"], 200)
        self.assertIsNone(results[1]["status"])
        self.assertEqual(results[1]["errors"], [{"step": "internal", "status": None}])
        self.assertEqual((results[1]["registered"], results[1]["deleted"]), ([], []))

    def test_isapi_request_reintenta_una_vez_si_el_keepalive_estaba_cerrado(self):
        ok = FakeResponse(200)
        for causa in (ProtocolError("Connection aborted.", ConnectionResetError(104, "reset")),
                      MaxRetryError(None, "/x", reason=ProtocolError("Connection aborted."))):
            with self.subTest(causa=type(causa).__name__):
                session = mock.Mock()
                session.request.side_effect = [requests.ConnectionError(causa), ok]
                with mock.patch.object(httpclient, "get_isapi_session", return_value=session):
                    self.assertIs(httpclient.isapi_request("POST", "http://10.0.0.5/ISAPI/x"), ok)
                self.assertEqual(session.request.call_count, 2)

    def test_isapi_request_no_reintenta_timeouts_ni_terminal_apagado(self):
        casos = (
            requests.ConnectTimeout(MaxRetryError(None, "/x", reason=ConnectTimeoutError(None, "t"))),
            requests.ReadTimeout("read"),
            requests.ConnectionError(MaxRetryError(None, "/x", reason=NewConnectionError(None, "unreachable"))),
        )
        for exc in casos:
            with self.subTest(exc=type(exc).__name__):
                session = mock.Mock()
                session.request.side_effect = exc
                with mock.patch.object(httpclient, "get_isapi_session", return_value=session):
                    with self.assertRaises(type(exc)):
                        httpclient.isapi_request("POST", "http://10.0.0.5/ISAPI/x")
                self.assertEqual(session.request.call_count, 1)

    def test_terminal_sin_ip_se_salta_y_no_rompe_el_orden(self):
        t = self.attach(FakeTerminal())
        results = self.service.update_card(card_no="A", employee_no=EMP,
                                           terminals=[{"ip": ""}, TERMINAL], card_nos=["A"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ip"], TERMINAL["ip"])
        self.assertEqual(t.cards_of(EMP), ["A"])

    def test_sin_terminales_devuelve_lista_vacia(self):
        self.attach(FakeTerminal())
        self.assertEqual(self.service.update_card(card_no="A", employee_no=EMP, terminals=[]), [])


class UpdateCardEndpointTests(HikvisionBase):
    """POST /update-card con create_app() real y todos los archivos sandboxeados."""

    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        for module, name in ((admin_auth, "CREDENTIALS_FILE"), (admin_auth, "SESSION_SECRET_FILE"),
                             (api_module, "DEVICE_ID_FILE"), (api_module, "DEVICE_TOKEN_FILE"),
                             (settings_module, "DEVICE_ID_FILE"), (settings_module, "DEVICE_TOKEN_FILE")):
            patcher = mock.patch.object(module, name, base / f"{module.__name__}.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        settings = Settings({"hikvision": {"enabled": True}}, "test")
        self.service = HikvisionService(settings, store=self.store)
        app = api_module.create_app(settings, gates=mock.Mock(),
                                    hikvision_service=self.service, screen=mock.Mock())
        app.testing = True
        self.client = app.test_client()

    def test_payload_nuevo_sincroniza_ventanas(self):
        t = self.attach(FakeTerminal())
        resp = self.client.post("/update-card", json={
            "employeeNo": EMP, "cardNo": "A", "cardNos": ["A", "B", "C"], "terminals": [TERMINAL]})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["cardNo"], "A")
        self.assertEqual(body["cardNos"], ["A", "B", "C"])
        self.assertEqual([strip_timing(r) for r in body["results"]],
                         [{"ip": TERMINAL["ip"], "status": 200, "employeeNo": EMP,
                           "registered": ["A", "B", "C"], "deleted": [], "errors": []}])
        self.assertEqual(t.cards_of(EMP), ["A", "B", "C"])

    def test_payload_legacy_sin_cardnos(self):
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, "X")]))
        resp = self.client.post("/update-card", json={
            "employeeNo": EMP, "cardNo": "A", "terminals": [TERMINAL]})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(sorted(body), ["cardNo", "elapsedMs", "results"])
        self.assertEqual(body["cardNo"], "A")
        self.assertEqual([strip_timing(r) for r in body["results"]],
                         [{"ip": TERMINAL["ip"], "status": 200, "employeeNo": EMP}])
        self.assertEqual(t.paths(), [USER_SEARCH, CARD_DELETE, CARD_RECORD])
        self.assertEqual(t.cards_of(EMP), ["A"])

    def test_elapsed_ms_presente_por_terminal_y_total(self):
        self.attach(FakeTerminal())
        body = self.client.post("/update-card", json={
            "employeeNo": EMP, "cardNo": "A", "cardNos": ["A", "B"], "terminals": [TERMINAL]}).get_json()
        self.assertIsInstance(body["elapsedMs"], int)
        self.assertGreaterEqual(body["elapsedMs"], 0)
        self.assertIsInstance(body["results"][0]["elapsedMs"], int)
        self.assertGreaterEqual(body["results"][0]["elapsedMs"], 0)

    def test_cardnos_sin_cardno_usa_el_vigente(self):
        self.attach(FakeTerminal())
        resp = self.client.post("/update-card", json={
            "employeeNo": EMP, "cardNos": ["X", "Y"], "terminals": [TERMINAL]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["cardNo"], "X")

    def test_payloads_invalidos(self):
        self.attach(FakeTerminal())
        for payload in ({"terminals": []},                              # ni cardNo ni cardNos
                        {"cardNo": "A"},                                # sin terminals
                        {"cardNo": "A", "cardNos": "A", "terminals": []},     # cardNos no es lista
                        {"cardNo": "A", "cardNos": [1, 2], "terminals": []},  # no son strings
                        {"cardNo": "A", "cardNos": ["", "  "], "terminals": []}):  # vacios
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/update-card", json=payload).status_code, 400)


if __name__ == "__main__":
    unittest.main()
