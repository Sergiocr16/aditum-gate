"""Lecturas repetidas de la misma placa (anpr_dedupe) y su integracion con
POST /anpr-event.

La camara ya no filtra la misma placa durante 1 minuto (para que una vuelta en
U cuente), asi que una sola pasada puede llegar como dos o tres eventos
seguidos. El Pi tiene que mandar UNO a Aditum.

Correr desde la raiz del repo (sin red):
    python3 -m unittest discover -s device/tests -t device -v
"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aditum_gate import admin_auth
from aditum_gate import api as api_module
from aditum_gate import settings as settings_module
from aditum_gate.anpr_dedupe import RecentPlateFilter
from aditum_gate.anpr_events import AnprEventStore
from aditum_gate.anpr_trace import AnprTraceStore
from aditum_gate.settings import SCHEMA_FILE, Settings

logging.getLogger("aditum").setLevel(logging.CRITICAL)

CAMERA_A = "192.168.100.2"
CAMERA_B = "192.168.100.3"


class FakeClock:
    """Reloj monotono controlado: el filtro mide la ventana con el."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def event_xml(plate, vehicle_list, uid, ip=CAMERA_A):
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>%s</ipAddress><dateTime>2026-11-07T08:15:30-06:00</dateTime><eventType>ANPR</eventType>
<channelName>Entrada</channelName>
<ANPR><licensePlate>%s</licensePlate><direction>forward</direction>
<confidenceLevel>90</confidenceLevel><vehicleListName>%s</vehicleListName></ANPR>
<UUID>%s</UUID></EventNotificationAlert>""" % (ip, plate, vehicle_list, uid)).encode()


class RecentPlateFilterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.filter = RecentPlateFilter(15, clock=self.clock)

    def test_primera_lectura_pasa_y_la_repetida_dice_cuanto_hace(self):
        self.assertIsNone(self.filter.repeated_within("ABC123", CAMERA_A))
        self.clock.advance(4)
        self.assertEqual(self.filter.repeated_within("ABC123", CAMERA_A), 4)

    def test_pasada_la_ventana_vuelve_a_contar(self):
        self.filter.repeated_within("ABC123", CAMERA_A)
        self.clock.advance(16)
        self.assertIsNone(self.filter.repeated_within("ABC123", CAMERA_A))

    def test_ventana_deslizante_un_carro_detenido_es_un_evento(self):
        # Lecturas cada 10 s durante 40 s: todas repetidas aunque la primera
        # quedo a 40 s, porque cada una renueva la ventana.
        self.assertIsNone(self.filter.repeated_within("ABC123", CAMERA_A))
        for _ in range(4):
            self.clock.advance(10)
            self.assertEqual(self.filter.repeated_within("ABC123", CAMERA_A), 10)
        self.clock.advance(16)
        self.assertIsNone(self.filter.repeated_within("ABC123", CAMERA_A))

    def test_otra_camara_es_otro_evento(self):
        self.assertIsNone(self.filter.repeated_within("ABC123", CAMERA_A))
        self.assertIsNone(self.filter.repeated_within("ABC123", CAMERA_B))
        # La camara con puerto es la misma camara
        self.assertEqual(self.filter.repeated_within("ABC123", CAMERA_A + ":8000"), 0)

    def test_la_placa_se_compara_normalizada(self):
        self.assertIsNone(self.filter.repeated_within("ABC-123", CAMERA_A))
        self.clock.advance(1)
        self.assertEqual(self.filter.repeated_within("abc 123", CAMERA_A), 1)
        self.assertIsNone(self.filter.repeated_within("ABC124", CAMERA_A))

    def test_ventana_cero_o_invalida_apaga_el_filtro(self):
        for window in (0, None, "abc", -5):
            flt = RecentPlateFilter(window, clock=self.clock)
            self.assertFalse(flt.enabled)
            self.assertIsNone(flt.repeated_within("ABC123", CAMERA_A))
            self.assertIsNone(flt.repeated_within("ABC123", CAMERA_A))

    def test_olvida_lo_que_salio_de_la_ventana(self):
        self.filter.repeated_within("AAA111", CAMERA_A)
        self.filter.repeated_within("BBB222", CAMERA_A)
        self.assertEqual(self.filter.tracked(), 2)
        self.clock.advance(16)
        self.assertEqual(self.filter.tracked(), 0)


class AnprEventDedupeTests(unittest.TestCase):
    """POST /anpr-event con create_app() real, archivos sandboxeados."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for module, name in ((admin_auth, "CREDENTIALS_FILE"), (admin_auth, "SESSION_SECRET_FILE"),
                             (api_module, "DEVICE_ID_FILE"), (api_module, "DEVICE_TOKEN_FILE"),
                             (settings_module, "DEVICE_ID_FILE"), (settings_module, "DEVICE_TOKEN_FILE")):
            patcher = mock.patch.object(module, name, base / f"{module.__name__}.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        self.base = base
        self.clock = FakeClock()
        self.store = AnprEventStore(base / "events.db")
        self.trace = AnprTraceStore(base / "events.db")
        self.uid = 0
        self.client = self.make_client(anpr_dedupe=RecentPlateFilter(15, clock=self.clock))

    def make_client(self, anpr_config=None, **kwargs):
        settings = Settings({"anpr": dict({"enabled": True}, **(anpr_config or {}))},
                            self.base / "config-runtime.json")
        app = api_module.create_app(
            settings, gates=mock.Mock(), hikvision_service=None, screen=mock.Mock(),
            anpr_store=self.store, anpr_trace=self.trace, **kwargs)
        app.testing = True
        return app.test_client()

    def read(self, plate, vehicle_list="allowList", ip=CAMERA_A, uid=None, client=None):
        if uid is None:
            self.uid += 1
            uid = f"uid-{self.uid}"
        resp = (client or self.client).post(
            "/anpr-event", data=event_xml(plate, vehicle_list, uid, ip),
            content_type="application/xml", environ_base={"REMOTE_ADDR": ip})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def outcomes(self, plate="ABC123"):
        return [r["outcome"] for r in self.trace.reads(plate)]

    # -- escenarios ----------------------------------------------------
    def test_la_segunda_lectura_en_la_ventana_no_se_encola(self):
        first = self.read("ABC123")
        self.assertTrue(first["queued"])
        self.clock.advance(5)
        second = self.read("ABC123")
        self.assertEqual(second, {"ignored": "placa repetida", "eventUid": "uid-2",
                                  "secondsSinceLast": 5.0, "windowSeconds": 15})
        stats = self.store.stats()
        self.assertEqual((stats["pending"], stats["duplicates"]), (1, 1))
        # La repetida no tapa la cola ni cuenta como encolada por el filtro
        self.assertEqual([r["event_uid"] for r in stats["recent"]], ["uid-1"])
        self.assertEqual(sum(l["queued"] for l in stats["lists"]), 1)
        self.assertEqual(self.outcomes(), ["queued", "duplicate"])

    def test_carro_detenido_en_la_aguja_es_un_solo_evento(self):
        self.read("ABC123")
        for _ in range(3):
            self.clock.advance(10)
            self.assertEqual(self.read("ABC123")["ignored"], "placa repetida")
        stats = self.store.stats()
        self.assertEqual((stats["pending"], stats["duplicates"]), (1, 3))
        # Silencio mayor que la ventana: otra pasada (vuelta en U)
        self.clock.advance(16)
        self.assertTrue(self.read("ABC123")["queued"])
        self.assertEqual(self.store.stats()["pending"], 2)

    def test_vuelta_en_u_despues_de_la_ventana_si_cuenta(self):
        self.read("ABC123")
        self.clock.advance(20)
        self.assertTrue(self.read("ABC123")["queued"])
        self.assertEqual(self.store.stats()["pending"], 2)

    def test_otra_camara_del_mismo_pi_es_otro_evento(self):
        self.read("ABC123", ip=CAMERA_A)
        self.clock.advance(2)
        self.assertTrue(self.read("ABC123", ip=CAMERA_B)["queued"])
        self.assertEqual(self.store.stats()["pending"], 2)

    def test_placa_con_guion_es_la_misma_placa(self):
        self.read("ABC-123")
        self.clock.advance(3)
        self.assertEqual(self.read("ABC123")["ignored"], "placa repetida")

    def test_una_descartada_no_arma_la_ventana(self):
        # otherList con onlyAuthorized (default): descartada. Si 2 s despues
        # la camara la lee en el allowlist (p. ej. recien agregada), se encola.
        self.assertEqual(self.read("ABC123", "otherList")["ignored"], "no autorizada")
        self.clock.advance(2)
        self.assertTrue(self.read("ABC123", "allowList")["queued"])
        self.assertEqual(self.outcomes(), ["discarded", "queued"])

    def test_reintento_de_la_camara_con_el_mismo_uuid_no_suma_fila(self):
        self.read("ABC123", uid="mismo-uuid")
        self.clock.advance(3)
        resp = self.read("ABC123", uid="mismo-uuid")
        self.assertEqual(resp["ignored"], "placa repetida")
        stats = self.store.stats()
        # INSERT OR IGNORE por event_uid: la fila pendiente sigue intacta
        self.assertEqual((stats["pending"], stats["duplicates"]), (1, 0))
        self.assertEqual(self.outcomes(), ["queued"])

    def test_ventana_cero_en_la_config_envia_todas(self):
        client = self.make_client({"duplicateWindowSeconds": 0})
        self.read("ABC123", client=client)
        self.assertTrue(self.read("ABC123", client=client)["queued"])
        self.assertEqual(self.store.stats()["pending"], 2)

    def test_sin_filtro_inyectado_se_arma_desde_la_config(self):
        # Default de settings (15 s) con el reloj real: dos posts seguidos
        # llegan con milisegundos de diferencia.
        client = self.make_client()
        self.read("ABC123", client=client)
        resp = self.read("ABC123", client=client)
        self.assertEqual((resp["ignored"], resp["windowSeconds"]), ("placa repetida", 15))

    def test_la_purga_se_lleva_las_repetidas_pero_no_lo_pendiente(self):
        self.read("ABC123")
        self.clock.advance(1)
        self.read("ABC123")
        conn = self.store._connect()
        try:
            conn.execute("UPDATE anpr_event SET received_at='2020-01-01T00:00:00-06:00'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.store.purge_processed(7), 1)
        stats = self.store.stats()
        self.assertEqual((stats["pending"], stats["duplicates"]), (1, 0))


class DuplicateWindowConfigTests(unittest.TestCase):
    def test_default_de_settings_y_schema_coinciden(self):
        settings = Settings({"anpr": {"enabled": True}}, "config-runtime.json")
        self.assertEqual(settings.anpr_duplicate_window_seconds, 15)
        prop = json.loads(Path(SCHEMA_FILE).read_text())["properties"]["anpr"]["properties"][
            "duplicateWindowSeconds"]
        self.assertEqual((prop["type"], prop["minimum"], prop["maximum"], prop["default"]),
                         ("integer", 0, 60, 15))

    def test_settings_lee_el_valor_configurado(self):
        settings = Settings({"anpr": {"enabled": True, "duplicateWindowSeconds": 10}}, "x.json")
        self.assertEqual(settings.anpr_duplicate_window_seconds, 10)


if __name__ == "__main__":
    unittest.main()
