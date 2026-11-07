"""Historial de la lista de placas de la camara (anpr_trace).

Fija como se registran las entradas y salidas de una placa en el allowlist
de la camara a partir de cada escritura del Pi (full sync, alta/baja suelta,
prueba manual), los cambios hechos por fuera (EXTERNAL), la linea base de la
primera escritura, los fallos, la membresia en un momento dado y la purga.

Correr desde la raiz del repo (sin red):
    python3 -m unittest discover -s device/tests -t device -v
"""
import copy
import logging
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from aditum_gate import anpr_trace
from aditum_gate.anpr import AnprCameraError, AnprService
from aditum_gate.anpr_events import parse_io_event_xml
from aditum_gate.anpr_trace import AnprTraceStore

logging.getLogger("aditum").setLevel(logging.CRITICAL)

HEADER = ["No.", "License Plate", "Group", "Start Date", "End Date", "Card No."]
IP = "192.168.100.2"
CAMERAS = [{"ip": IP, "user": "admin", "password": "secreto"}]


def row(plate, group="1"):
    return ["", plate, group, "2000-01-01", "2046-01-01", ""]


class FakeCamera:
    """Lista de placas en memoria detras de get/put_plate_rows."""

    def __init__(self, rows=()):
        self.rows = [list(r) for r in rows]
        self.fail_put = None

    def get(self, ip, user, password, timeout=None):
        return list(HEADER), copy.deepcopy(self.rows)

    def put(self, ip, user, password, header, rows, timeout=None):
        if self.fail_put:
            raise AnprCameraError(self.fail_put, "simulado")
        self.rows = copy.deepcopy(rows)
        return True


class Clock:
    """Reemplaza anpr_trace._now para fijar la hora de cada registro."""

    def __init__(self, ts):
        self.ts = ts

    def __call__(self):
        return datetime.fromtimestamp(self.ts).astimezone().isoformat(timespec="seconds"), self.ts

    def advance(self, seconds):
        self.ts += seconds


class TraceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trace = AnprTraceStore(Path(self.tmp.name) / "events.db", retention_days=90)
        self.clock = Clock(time.time() - 10 * 86400)
        patcher = mock.patch.object(anpr_trace, "_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.camera = FakeCamera()
        self.service = AnprService(trace=self.trace)
        for name, fake in (("get_plate_rows", self.camera.get),
                           ("put_plate_rows", self.camera.put)):
            patcher = mock.patch.object(self.service.client, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        mock.patch.object(self.service.client, "plate_capacity", return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def sync(self, plates, request_id="r1", camera_id=7):
        return self.service.sync_plates(plates, CAMERAS,
                                        context={"requestId": request_id, "cameraId": camera_id})

    def changes(self, plate):
        return [(c["change"], c["source"]) for c in self.trace.plate_changes(plate)]


class ListChangesTest(TraceTestCase):

    def test_primera_escritura_registra_la_lista_previa_como_baseline(self):
        self.camera.rows = [row("AAA111"), row("BBB222")]
        self.sync(["BBB222", "CCC333"])
        self.assertEqual(self.changes("AAA111"), [("ADDED", "BASELINE"), ("REMOVED", "SYNC")])
        self.assertEqual(self.changes("BBB222"), [("ADDED", "BASELINE")])
        self.assertEqual(self.changes("CCC333"), [("ADDED", "SYNC")])
        log = self.trace.sync_log()
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["kind"], log[0]["ok"], log[0]["added"], log[0]["removed"],
                          log[0]["camera_id"], log[0]["request_id"]),
                         ("FULL_SYNC", 1, 1, 1, "7", "r1"))

    def test_cambio_hecho_por_fuera_se_registra_como_external(self):
        self.sync(["AAA111", "BBB222"])
        # Alguien agrega una placa y borra otra directo en la camara
        self.camera.rows = [row("AAA111"), row("XXX999")]
        self.clock.advance(60)
        self.service.update_plate("ADD", "CCC333", CAMERAS,
                                  context={"requestId": "r2", "cameraId": 7})
        self.assertEqual(self.changes("XXX999"), [("ADDED", "EXTERNAL")])
        self.assertEqual(self.changes("BBB222"), [("ADDED", "SYNC"), ("REMOVED", "EXTERNAL")])
        self.assertEqual(self.changes("CCC333"), [("ADDED", "UPDATE")])
        self.assertEqual(self.trace.sync_log()[-1]["external"], 2)

    def test_noop_no_registra_cambios_pero_si_el_envio(self):
        self.sync(["AAA111"])
        self.service.update_plate("ADD", "AAA111", CAMERAS)
        self.assertEqual(self.changes("AAA111"), [("ADDED", "SYNC")])
        self.assertEqual([r["kind"] for r in self.trace.sync_log()], ["FULL_SYNC", "ADD"])

    def test_lista_negra_no_cuenta_como_allowlist(self):
        self.camera.rows = [row("NEG111", group="0")]
        self.sync(["AAA111"])
        self.assertEqual(self.changes("NEG111"), [])

    def test_escritura_fallida_queda_como_error_sin_cambios(self):
        self.camera.fail_put = "CAMERA_TIMEOUT"
        results, error = self.sync(["AAA111"])
        self.assertEqual(error, "CAMERA_TIMEOUT")
        errors = self.trace.sync_log(only_errors=True)
        self.assertEqual([(e["kind"], e["error_code"]) for e in errors],
                         [("FULL_SYNC", "CAMERA_TIMEOUT")])
        self.assertEqual(self.changes("AAA111"), [])

    def test_prueba_manual_queda_como_test(self):
        self.sync(["AAA111"])
        self.service.apply_test_plate(IP, "admin", "x", "DELETE", "AAA111")
        self.assertEqual(self.changes("AAA111"), [("ADDED", "SYNC"), ("REMOVED", "TEST")])
        self.assertEqual(self.trace.sync_log()[-1]["kind"], "TEST_DELETE")

    def test_si_el_historial_falla_la_sincronizacion_sigue(self):
        with mock.patch.object(self.trace, "record_write", side_effect=RuntimeError("disco")):
            results, error = self.sync(["AAA111"])
        self.assertIsNone(error)
        self.assertEqual(self.camera.rows[0][1], "AAA111")


class MembershipTest(TraceTestCase):

    def test_membresia_en_el_tiempo(self):
        t_before = self.clock.ts - 1
        self.sync(["AAA111"])
        t_added = self.clock.ts
        self.clock.advance(3600)
        self.sync([])
        t_removed = self.clock.ts
        self.assertEqual(self.trace.membership_at(IP, "AAA111", t_before), (None, None))
        state, change = self.trace.membership_at(IP, "AAA111", t_added + 10)
        self.assertTrue(state)
        self.assertEqual(change["source"], "SYNC")
        self.assertEqual(self.trace.membership_at(IP, "AAA111", t_removed + 1)[0], False)
        # Placa que nunca estuvo, con historial de la camara: False (no None)
        self.assertEqual(self.trace.membership_at(IP, "ZZZ000", t_added + 10), (False, None))

    def test_camera_id_resuelve_la_ip(self):
        self.sync(["AAA111"], camera_id=42)
        self.assertEqual(self.trace.camera_ips_for_id(42), [IP])
        self.assertEqual(self.trace.camera_ips_for_id(99), [])

    def test_purga_conserva_el_estado_al_corte(self):
        self.sync(["AAA111", "BBB222"])            # hace 10 dias
        self.clock.advance(86400)
        self.sync(["AAA111"])                      # hace 9 dias: sale BBB222
        self.clock.ts = time.time()
        self.trace.purge(days=7)
        self.assertEqual(self.trace.sync_log(), [])
        self.assertTrue(self.trace.membership_at(IP, "AAA111", time.time())[0])
        self.assertFalse(self.trace.membership_at(IP, "BBB222", time.time())[0])
        # La lista conocida sigue completa: la proxima escritura no la ve como EXTERNAL
        self.sync(["AAA111"])
        self.assertEqual(self.trace.sync_log()[-1]["external"], 0)


class ReadAndIoTest(TraceTestCase):

    def test_lectura_normaliza_y_es_idempotente_por_uid(self):
        event = {"eventUid": "u1", "licensePlate": "abc-123", "capturedAt": "2026-11-06T08:00:00-06:00",
                 "vehicleList": "otherlist", "confidenceLevel": 80, "cameraName": "Entrada",
                 "direction": "forward"}
        self.trace.record_read(event, IP, "discarded", {"name": "x.json", "images": ["x_0-a.jpg"]})
        self.trace.record_read(event, IP, "discarded")
        reads = self.trace.reads("ABC123")
        self.assertEqual(len(reads), 1)
        self.assertEqual((reads[0]["plate_raw"], reads[0]["photos"]), ("abc-123", "x_0-a.jpg"))

    def test_lectura_ilegible_se_guarda_sin_placa(self):
        self.trace.record_read({"eventUid": "u2", "licensePlate": None}, IP, "unreadable")
        self.assertEqual(self.trace.reads("ABC123"), [])

    def test_evento_io_de_la_camara(self):
        xml = (b'<EventNotificationAlert><eventType>IO</eventType><eventState>active</eventState>'
               b'<inputIOPortID>1</inputIOPortID><dateTime>2026-11-06T08:00:00-06:00</dateTime>'
               b'</EventNotificationAlert>')
        io = parse_io_event_xml(xml)
        self.assertEqual((io["eventType"], io["port"], io["state"]), ("IO", "1", "active"))
        self.trace.record_io(io, IP)
        self.assertEqual(len(self.trace.io_events([IP])), 1)
        self.assertIsNone(parse_io_event_xml(b"<EventNotificationAlert><eventType>ANPR</eventType>"
                                             b"</EventNotificationAlert>"))


if __name__ == "__main__":
    unittest.main()
