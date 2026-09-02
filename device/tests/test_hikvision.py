"""Tests de la sincronizacion de tarjetas Hikvision (modo ventanas y legacy).

Correr desde la raiz del repo (sin red: el ISAPI se simula):
    python3 -m unittest discover -s device/tests -t device -v
"""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aditum_gate import admin_auth, hikvision
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

    def __init__(self, users=(), cards=(), auth_ok=True, card_search_status=200):
        self.users = set(users)
        self.cards = list(cards)  # [(employeeNo, cardNo)] en orden de registro
        self.auth_ok = auth_ok
        self.card_search_status = card_search_status
        self.calls = []  # (method, path, payload)

    def cards_of(self, emp):
        return [c for e, c in self.cards if e == emp]

    def paths(self):
        return [p for _, p, _ in self.calls]

    def request(self, method, url, timeout=None, json=None, auth=None, **kwargs):
        path = url.split("/ISAPI/", 1)[1].split("?")[0]
        self.calls.append((method, path, json))
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
        raise AssertionError(f"ISAPI inesperado: {method} {path}")


class HikvisionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CardStore(Path(self.tmp.name) / "cards.json")
        self.service = HikvisionService(mock.Mock(nightly_cleanup_hour=2), store=self.store)

    def attach(self, terminal):
        patcher = mock.patch.object(hikvision.httpclient, "request", side_effect=terminal.request)
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
        self.assertEqual(r, {"ip": TERMINAL["ip"], "status": 200, "employeeNo": EMP})
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

        patcher = mock.patch.object(hikvision.httpclient, "request", side_effect=down)
        patcher.start()
        self.addCleanup(patcher.stop)
        r = self.sync(["A", "B"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["status"], None)
        self.assertEqual(r["errors"], [{"step": "ensure_user", "status": None}])


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
        self.assertEqual(body["results"], [{"ip": TERMINAL["ip"], "status": 200, "employeeNo": EMP,
                                            "registered": ["A", "B", "C"], "deleted": [], "errors": []}])
        self.assertEqual(t.cards_of(EMP), ["A", "B", "C"])

    def test_payload_legacy_sin_cardnos(self):
        t = self.attach(FakeTerminal(users=[EMP], cards=[(EMP, "X")]))
        resp = self.client.post("/update-card", json={
            "employeeNo": EMP, "cardNo": "A", "terminals": [TERMINAL]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"cardNo": "A", "results": [
            {"ip": TERMINAL["ip"], "status": 200, "employeeNo": EMP}]})
        self.assertEqual(t.paths(), [USER_SEARCH, CARD_DELETE, CARD_RECORD])
        self.assertEqual(t.cards_of(EMP), ["A"])

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
