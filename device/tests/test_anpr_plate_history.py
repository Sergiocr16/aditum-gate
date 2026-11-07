"""POST /anpr-plate-history: el recorrido completo de una placa.

Escenarios del problema real: una placa leida como otherList que deberia
haber estado en el allowlist. El endpoint tiene que decir con evidencia si
estaba en la lista de la camara al leerla, cuando entro/salio, si fallo una
sincronizacion antes, si la camara leyo mal un caracter y si hay evidencia
de apertura.

Correr desde la raiz del repo (sin red):
    python3 -m unittest discover -s device/tests -t device -v
"""
import logging
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from aditum_gate import admin_auth, anpr_camera_diag, anpr_trace
from aditum_gate import api as api_module
from aditum_gate import settings as settings_module
from aditum_gate.anpr import AnprCameraError, AnprService
from aditum_gate.anpr_captures import AnprCaptureStore
from aditum_gate.anpr_events import AnprEventStore
from aditum_gate.anpr_trace import AnprTraceStore
from aditum_gate.settings import Settings

from tests.test_anpr_trace import HEADER, Clock, FakeCamera, row

logging.getLogger("aditum").setLevel(logging.CRITICAL)

IP = "192.168.100.2"
CAMERAS = [{"ip": IP, "user": "admin", "password": "secreto"}]


def event_xml(plate, vehicle_list, when_iso, uid):
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>%s</ipAddress><dateTime>%s</dateTime><eventType>ANPR</eventType>
<channelName>Entrada</channelName>
<ANPR><licensePlate>%s</licensePlate><direction>forward</direction>
<confidenceLevel>90</confidenceLevel><vehicleListName>%s</vehicleListName></ANPR>
<UUID>%s</UUID></EventNotificationAlert>""" % (IP, when_iso, plate, vehicle_list, uid)).encode()


class PlateHistoryApiTest(unittest.TestCase):
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
        self.clock = Clock(time.time() - 3 * 86400)
        mock.patch.object(anpr_trace, "_now", self.clock).start()
        self.addCleanup(mock.patch.stopall)

        self.trace = AnprTraceStore(base / "events.db")
        self.service = AnprService(trace=self.trace)
        self.camera = FakeCamera()
        mock.patch.object(self.service.client, "get_plate_rows", side_effect=self.camera.get).start()
        mock.patch.object(self.service.client, "put_plate_rows", side_effect=self.camera.put).start()
        mock.patch.object(self.service.client, "plate_capacity", return_value=None).start()
        settings = Settings({"anpr": {"enabled": True}}, base / "config-runtime.json")
        app = api_module.create_app(
            settings, gates=mock.Mock(), hikvision_service=None, screen=mock.Mock(),
            anpr_service=self.service, anpr_store=AnprEventStore(base / "events.db"),
            anpr_captures=AnprCaptureStore(base / "caps", retention_days=15, max_mb=50),
            anpr_trace=self.trace)
        app.testing = True
        self.client = app.test_client()
        self.uid = 0

    # -- helpers -------------------------------------------------------
    def sync(self, plates, request_id="r1"):
        resp = self.client.post("/sync-plates", json={
            "requestId": request_id, "cameraId": 7, "cameras": CAMERAS,
            "plates": [{"plate": p, "plateNormalized": p} for p in plates]})
        return resp

    def read(self, plate, vehicle_list, captured_ago=0):
        self.uid += 1
        when = datetime.fromtimestamp(self.clock.ts - captured_ago).astimezone() \
            .isoformat(timespec="seconds")
        resp = self.client.post("/anpr-event", data=event_xml(plate, vehicle_list, when, f"uid-{self.uid}"),
                                content_type="application/xml",
                                environ_base={"REMOTE_ADDR": IP})
        self.assertEqual(resp.status_code, 200)

    def history(self, **body):
        body.setdefault("plate", "ABC123")
        resp = self.client.post("/anpr-plate-history", json=body)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    @staticmethod
    def codes(result):
        return {f["code"] for f in result["findings"]}

    # -- escenarios ----------------------------------------------------
    def test_sync_plates_devuelve_lo_que_cambio(self):
        self.sync(["AAA111"])
        body = self.sync(["BBB222"], request_id="r2").get_json()
        self.assertEqual({k: body["results"][0][k] for k in ("added", "removed", "external")},
                         {"added": 1, "removed": 1, "external": 0})

    def test_otherlist_estando_en_el_allowlist(self):
        self.sync(["ABC123"])
        self.clock.advance(3600)
        self.read("ABC123", "otherList")
        result = self.history()
        self.assertIn("OTHERLIST_WHILE_ON_ALLOWLIST", self.codes(result))
        read = result["reads"][0]
        self.assertEqual((read["list"], read["onAllowlistAtThatTime"]), ("otherlist", True))
        self.assertEqual(read["listChangeBefore"]["source"], "SYNC")
        self.assertEqual(result["summary"]["otherListWhileOnAllowlist"], 1)
        self.assertEqual(result["summary"]["lastAdded"]["requestId"], "r1")
        self.assertTrue(result["cameras"][0]["onListNow"])

    def test_leida_antes_de_entrar_a_la_lista(self):
        self.sync(["ZZZ999"])
        self.clock.advance(600)
        self.read("ABC123", "otherList")
        self.clock.advance(1800)
        self.sync(["ZZZ999", "ABC123"], request_id="r2")
        result = self.history()
        finding = next(f for f in result["findings"] if f["code"] == "NOT_ON_LIST_AT_READ")
        self.assertIn("todavía no había entrado", finding["message"])
        self.assertIn("entró después", finding["message"])
        self.assertFalse(result["reads"][0]["onAllowlistAtThatTime"])
        self.assertEqual(result["summary"]["timesAdded"], 1)

    def test_la_sacaron_antes_de_la_lectura(self):
        self.sync(["ABC123"])
        self.clock.advance(600)
        self.sync([], request_id="r2")
        self.clock.advance(600)
        self.read("ABC123", "otherList")
        result = self.history()
        finding = next(f for f in result["findings"] if f["code"] == "NOT_ON_LIST_AT_READ")
        self.assertIn("la habían sacado", finding["message"])
        self.assertEqual(result["summary"]["lastRemoved"]["requestId"], "r2")
        self.assertEqual([c["change"] for c in result["listHistory"]], ["ADDED", "REMOVED"])

    def test_sync_fallido_antes_de_la_lectura(self):
        self.sync(["ZZZ999"])
        self.clock.advance(60)
        self.camera.fail_put = "CAMERA_TIMEOUT"
        self.assertEqual(self.sync(["ZZZ999", "ABC123"], request_id="r2").status_code, 504)
        self.clock.advance(60)
        self.read("ABC123", "otherList")
        result = self.history()
        self.assertIn("SYNC_FAILED_BEFORE_READ", self.codes(result))
        self.assertEqual(result["syncErrors"][0]["error"], "CAMERA_TIMEOUT")

    def test_lectura_parecida_por_mala_lectura(self):
        self.sync(["ABC123"])
        self.clock.advance(60)
        self.read("A8C123", "otherList")
        self.read("QQQ111", "otherList")
        result = self.history()
        self.assertEqual([s["plate"] for s in result["similarReads"]], ["A8C123"])
        self.assertIn("POSSIBLE_MISREAD", self.codes(result))
        self.assertIn("NO_READS", self.codes(result))

    def test_sin_historial_previo_al_despliegue(self):
        self.read("ABC123", "otherList")
        self.clock.advance(60)
        self.sync(["ABC123"])
        result = self.history()
        self.assertEqual(result["reads"][0]["onAllowlistAtThatTime"], "unknown")
        self.assertIn("NO_HISTORY", self.codes(result))

    def test_lectura_reenviada_tarde_por_anr(self):
        # Se capturo con la placa en la lista; el Pi la recibio media hora
        # despues (ANR), cuando ya la habian sacado.
        self.sync(["ABC123"])
        self.clock.advance(600)
        capture_ts = self.clock.ts
        self.clock.advance(600)
        self.sync([], request_id="r2")
        self.clock.advance(1200)
        self.read("ABC123", "allowList", captured_ago=self.clock.ts - capture_ts)
        read = self.history()["reads"][0]
        self.assertEqual(read["onAllowlistAtThatTime"], False)
        self.assertEqual(read["onAllowlistAtCaptureTime"], True)
        self.assertAlmostEqual(read["clockSkewSeconds"], 1800, delta=1)

    def test_lectura_en_allowlist_con_sensor_de_apertura(self):
        self.sync(["ABC123"])
        self.clock.advance(60)
        self.read("ABC123", "allowList")
        self.clock.advance(3)
        io = (b"<EventNotificationAlert><eventType>IO</eventType><eventState>active</eventState>"
              b"<inputIOPortID>1</inputIOPortID></EventNotificationAlert>")
        resp = self.client.post("/anpr-event", data=io, content_type="application/xml",
                                environ_base={"REMOTE_ADDR": IP})
        self.assertEqual(resp.get_json(), {"recorded": "io"})
        read = self.history()["reads"][0]
        self.assertEqual((read["expectedOpen"], read["opened"], read["openStage"]),
                         (True, True, "PHYSICAL"))

    def test_sin_credenciales_la_apertura_queda_desconocida(self):
        self.sync(["ABC123"])
        self.clock.advance(60)
        self.read("ABC123", "allowList")
        result = self.history()
        self.assertEqual(result["reads"][0]["opened"], "unknown")
        self.assertEqual(result["summary"]["readsWithoutOpenEvidence"], 1)
        self.assertNotIn("live", result["cameras"][0])

    def test_log_de_la_camara_confirma_o_niega_la_orden_de_apertura(self):
        self.sync(["ABC123"])
        self.clock.advance(60)
        self.read("ABC123", "allowList")
        read_ts = self.clock.ts
        time_ok = {"supported": True, "ok": True, "skewSeconds": 0}
        with mock.patch.object(self.service.client, "find_plate",
                               return_value={"found": True, "total": 1,
                                             "match": {"plate": "ABC123", "group": "allowlist",
                                                       "validFrom": "2000-01-01", "validTo": "2046-01-01"}}), \
                mock.patch.object(anpr_camera_diag, "camera_time", return_value=time_ok), \
                mock.patch.object(anpr_camera_diag, "search_logs", return_value={
                    "supported": True, "ok": True, "entries": [
                        {"time": "x", "ts": read_ts + 2, "type": "Alarm Output Start",
                         "alarmOut": True, "alarmIn": False, "port": "1"}]}):
            result = self.history(cameraIp=IP, cameras=CAMERAS)
        self.assertEqual(result["reads"][0]["opened"], True)
        self.assertEqual(result["reads"][0]["openStage"], "OPEN_ORDER")
        self.assertTrue(result["cameras"][0]["live"]["find"]["found"])
        with mock.patch.object(self.service.client, "find_plate", side_effect=AnprCameraError("CAMERA_TIMEOUT")), \
                mock.patch.object(anpr_camera_diag, "camera_time", return_value=time_ok), \
                mock.patch.object(anpr_camera_diag, "search_logs",
                                  return_value={"supported": True, "ok": True, "entries": []}):
            result = self.history(cameraIp=IP, cameras=CAMERAS)
        self.assertEqual(result["reads"][0]["opened"], False)
        self.assertIn("READ_ALLOWLIST_NOT_OPENED", self.codes(result))
        self.assertEqual(result["cameras"][0]["live"]["find"]["error"], "CAMERA_TIMEOUT")
        # Camara sin SD (caso real): no se puede confirmar, sin acusar falla
        with mock.patch.object(self.service.client, "find_plate", side_effect=AnprCameraError("CAMERA_TIMEOUT")), \
                mock.patch.object(anpr_camera_diag, "camera_time", return_value=time_ok), \
                mock.patch.object(anpr_camera_diag, "search_logs",
                                  return_value={"supported": False, "ok": False, "error": "NO_STORAGE",
                                                "entries": []}):
            result = self.history(cameraIp=IP, cameras=CAMERAS)
        self.assertEqual(result["reads"][0]["opened"], "unknown")
        self.assertIn("SD", result["reads"][0]["openEvidence"])
        self.assertNotIn("READ_ALLOWLIST_NOT_OPENED", self.codes(result))

    def test_rechazada_sin_apertura_no_es_un_evento_de_apertura(self):
        self.read("ABC123", "otherList")
        with mock.patch.object(self.service.client, "find_plate",
                               side_effect=AnprCameraError("CAMERA_TIMEOUT")), \
                mock.patch.object(anpr_camera_diag, "camera_time",
                                  return_value={"supported": True, "ok": True, "skewSeconds": 0}), \
                mock.patch.object(anpr_camera_diag, "search_logs",
                                  return_value={"supported": True, "ok": True, "entries": []}):
            result = self.history(cameraIp=IP, cameras=CAMERAS)
        self.assertEqual([e["stage"] for e in result["timeline"]], ["READ"])
        self.assertFalse(result["timeline"][0]["expectedOpen"])
        self.assertNotIn("READ_ALLOWLIST_NOT_OPENED", self.codes(result))

    def test_camara_caida_no_rompe_la_consulta(self):
        self.sync(["ABC123"])
        self.clock.advance(60)
        self.read("ABC123", "allowList")
        with mock.patch.object(self.service.client, "find_plate",
                               side_effect=AnprCameraError("CAMERA_UNREACHABLE")), \
                mock.patch.object(self.service.client, "isapi",
                                  side_effect=AnprCameraError("CAMERA_UNREACHABLE")):
            result = self.history(cameraIp=IP, cameras=CAMERAS)
        live = result["cameras"][0]["live"]
        self.assertEqual(live["find"]["error"], "CAMERA_UNREACHABLE")
        self.assertEqual(live["time"]["error"], "CAMERA_UNREACHABLE")
        self.assertEqual(result["reads"][0]["opened"], "unknown")

    def test_camera_id_y_validaciones(self):
        self.sync(["ABC123"])
        self.assertEqual(self.history(cameraId=7)["cameras"][0]["ip"], IP)
        resp = self.client.post("/anpr-plate-history", json={"plate": "ABC123", "cameraId": 99})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post("/anpr-plate-history", json={})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/anpr-plate-history", json={
            "plate": "ABC123", "cameras": [{"ip": "8.8.8.8", "user": "a", "password": "b"}]})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/anpr-plate-history", json={
            "plate": "ABC123", "from": "2026-11-06", "to": "2026-11-01"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/anpr-plate-history", json={"plate": "ABC123", "from": "ayer"})
        self.assertEqual(resp.status_code, 400)

    def test_rango_por_fecha_filtra_lecturas(self):
        self.sync(["ABC123"])
        self.read("ABC123", "allowList")
        day = datetime.fromtimestamp(self.clock.ts).strftime("%Y-%m-%d")
        self.assertEqual(len(self.history(**{"from": day, "to": day})["reads"]), 1)
        self.assertEqual(len(self.history(**{"from": "2020-01-01", "to": "2020-01-31"})["reads"]), 0)

    def test_probe_rechaza_ip_publica_y_reporta_capacidades(self):
        resp = self.client.post("/anpr-plate-history/probe", json={"ip": "8.8.8.8"})
        self.assertEqual(resp.status_code, 400)
        fake = mock.Mock(status_code=200, content=b"<DeviceInfo><model>iDS-2CD7A46G0</model>"
                                                  b"<firmwareVersion>V5.7</firmwareVersion></DeviceInfo>",
                         text="<DeviceInfo/>")
        with mock.patch.object(self.service.client, "isapi", return_value=fake):
            resp = self.client.post("/anpr-plate-history/probe",
                                    json={"ip": IP, "user": "admin", "password": "x"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["summary"]["model"], "iDS-2CD7A46G0")


class PlateHistoryDisabledTest(unittest.TestCase):
    def test_sin_anpr_responde_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings({}, Path(tmp) / "config-runtime.json")
            with mock.patch.object(api_module, "DEVICE_TOKEN_FILE", Path(tmp) / "t"), \
                    mock.patch.object(settings_module, "DEVICE_TOKEN_FILE", Path(tmp) / "t"):
                app = api_module.create_app(settings, gates=mock.Mock(), hikvision_service=None,
                                            screen=mock.Mock())
                app.testing = True
                resp = app.test_client().post("/anpr-plate-history", json={"plate": "ABC123"})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()


# Respuestas reales de una iDS-2CD7A46G0/P-IZHSY, firmware V5.8.61 build 260327
# (camara de prueba sin SD), capturadas con docs/anpr-validacion-camara-real.md.
REAL_CAMERA = {
    "/ISAPI/System/deviceInfo": (200, "<DeviceInfo><deviceName>IP CAMERA</deviceName>"
                                      "<model>iDS-2CD7A46G0/P-IZHSY</model>"
                                      "<firmwareVersion>V5.8.61</firmwareVersion></DeviceInfo>"),
    "/ISAPI/System/time": (200, "<Time><timeMode>NTP</timeMode>"
                                "<localTime>2026-11-06T18:03:27-06:00</localTime>"
                                "<timeZone>CST+6:00:00</timeZone></Time>"),
    "/ISAPI/System/IO/capabilities": (200, "<IOCap><IOInputPortNums>2</IOInputPortNums>"
                                           "<IOOutputPortNums>2</IOOutputPortNums></IOCap>"),
    "/ISAPI/System/IO/outputs": (200, "<IOOutputPortList>"
        "<IOOutputPort><id>1</id><PowerOnState><defaultState>low</defaultState><outputState>pulse</outputState>"
        "<pulseDuration>1000</pulseDuration></PowerOnState><name></name></IOOutputPort>"
        "<IOOutputPort><id>2</id><PowerOnState><defaultState>low</defaultState><outputState>pulse</outputState>"
        "<pulseDuration>5000</pulseDuration></PowerOnState><name></name></IOOutputPort></IOOutputPortList>"),
    "/ISAPI/System/IO/inputs": (200, "<IOInputPortList>"
        "<IOInputPort><id>1</id><enabled>true</enabled><triggering>high</triggering></IOInputPort>"
        "<IOInputPort><id>2</id><enabled>true</enabled><triggering>high</triggering></IOInputPort>"
        "</IOInputPortList>"),
    "/ISAPI/Event/triggers/vehicledetection-1": (200, "<EventTrigger><id>vehicledetection-1</id>"
        "<eventType>vehicledetection</eventType><EventTriggerNotificationList><EventTriggerNotification>"
        "<id>center</id><notificationMethod>center</notificationMethod>"
        "<notificationRecurrence>beginning</notificationRecurrence>"
        "</EventTriggerNotification></EventTriggerNotificationList></EventTrigger>"),
    "/ISAPI/Event/notification/httpHosts": (200, "<HttpHostNotificationList><HttpHostNotification>"
        "<id>1</id><url>/anpr-event</url><protocolType>HTTP</protocolType>"
        "<ipAddress>192.168.100.223</ipAddress><portNo>80</portNo>"
        "</HttpHostNotification></HttpHostNotificationList>"),
    "/ISAPI/ContentMgmt/Storage": (200, '<storage version="2.0"><hddList size="8"></hddList>'
        '<nasList><supportMountType opt="NFS,SMB/CIFS"/></nasList><workMode>quota</workMode></storage>'),
    "/ISAPI/ContentMgmt/logSearch/capabilities": (200, '<LogSearchCap version="2.0"><Alarm>'
        '<minorType opt="netAlarmStart,netAlarmStopalarmIn,alarmOut,motdetStart,motdetStop"/>'
        '</Alarm></LogSearchCap>'),
    "/ISAPI/ContentMgmt/logSearch": (403, '<ResponseStatus version="2.0"><statusCode>4</statusCode>'
        '<statusString>Invalid Operation</statusString><subStatusCode>invalidMemoryCardOrNAS</subStatusCode>'
        '</ResponseStatus>'),
    "/ISAPI/Parking/channels/1/barrierGate": (404, '<ResponseStatus><statusCode>4</statusCode>'
        "<statusString>Invalid Operation</statusString><subStatusCode>notSupport</subStatusCode>"
        "<description>Can't find process for service:/ISAPI/Parking/channels/1/barrierGate</description>"
        "</ResponseStatus>"),
}


class FakeIsapiClient:
    """AnprCameraClient con las respuestas reales de REAL_CAMERA."""

    def isapi(self, method, ip, user, password, path, timeout=None, **kwargs):
        status, body = REAL_CAMERA.get(path, (404, "<ResponseStatus><statusCode>4</statusCode>"
                                                   "</ResponseStatus>"))
        return mock.Mock(status_code=status, content=body.encode(), text=body)


class RealCameraFormatsTest(unittest.TestCase):

    def test_log_sin_sd_se_reporta_no_storage(self):
        now = datetime.now()
        result = anpr_camera_diag.search_logs(FakeIsapiClient(), IP, "admin", "x", now, now)
        self.assertEqual((result["supported"], result["error"], result["reason"]),
                         (False, "NO_STORAGE", "invalidMemoryCardOrNAS"))

    def test_sonda_con_la_camara_real(self):
        report = anpr_camera_diag.probe(FakeIsapiClient(), IP, "admin", "x")
        s = report["summary"]
        self.assertEqual(s["model"], "iDS-2CD7A46G0/P-IZHSY")
        self.assertEqual((s["alarmOutputs"], s["alarmOutputPulseMs"], s["alarmInputs"]),
                         (2, ["1000", "5000"], 2))
        # Abre con su allowlist, pero el linkage no aparece en este trigger
        self.assertIsNone(s["linkageToAlarmOut"])
        self.assertEqual(s["postsTo"], [{"url": "/anpr-event", "ip": "192.168.100.223",
                                         "port": "80", "protocol": "HTTP"}])
        self.assertFalse(s["hasStorage"])                # hddList vacio
        self.assertTrue(s["logKnowsAlarmOut"])
        self.assertEqual(s["logSearchError"], "NO_STORAGE")
        self.assertFalse(s["canConfirmOpenOrder"])
        self.assertTrue(s["canConfirmPhysicalOpen"])
        self.assertFalse(s["barrierGateApi"])
        self.assertIs(report["barrierGate"]["supported"], False)   # 404: el firmware no lo tiene

    def test_alarm_out_matchea_el_minor_type_real(self):
        self.assertTrue(anpr_camera_diag._ALARM_OUT.search("log.std-cgi.com/Alarm alarmOut"))
        self.assertFalse(anpr_camera_diag._ALARM_OUT.search("alarmIn"))
