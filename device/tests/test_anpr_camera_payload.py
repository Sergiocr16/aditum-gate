"""Recepcion de la foto que postea la camara ANPR (POST /anpr-event).

Replica BYTE A BYTE el multipart que arma el alarm server de una camara
Hikvision ANPR (boundary literal "boundary", partes anpr.xml +
licensePlatePicture.jpg + detectionPicture.jpg, Content-Length por parte,
CRLF) y comprueba que los JPG llegan intactos a la bitacora de no
reconocidas, con y sin `filename=` en Content-Disposition (Werkzeug solo
trata como archivo las partes CON filename; sin el, el respaldo crudo).

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
from aditum_gate.anpr_captures import AnprCaptureStore
from aditum_gate.anpr_events import AnprEventStore
from aditum_gate.settings import Settings

CAMERA_IP = "192.168.100.106"

# Solo WARNING hacia arriba (assertLogs necesita el logger vivo; el INFO de
# "credenciales sembradas" y de cada evento es ruido en el reporte)
logging.getLogger("aditum").setLevel(logging.WARNING)

# Binario "hostil": todos los bytes, saltos CRLF sueltos, guiones dobles y
# un pseudo-boundary parcial. Si el parser toca algo, la comparacion falla.
LICENSE_JPG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(256)) * 24
               + b"\r\n--bound\r\n\r\n--\r\n" + bytes(range(255, -1, -1)) * 8 + b"\xff\xd9")
# La escena pesa mas de 500 KB a proposito: es el default de
# MAX_FORM_MEMORY_SIZE de Werkzeug, que sin filename= trataria el JPG como
# campo de formulario y responderia 413 (create_app lo sube).
DETECTION_JPG = (b"\xff\xd8\xff\xe1" + bytes(range(256)) * 2400 + b"\r\n\r\n--x--\r\n"
                 + b"\x00" * 1000 + b"\xff\xd9")


def hikvision_xml(plate="unknown", vehicle_list="otherList", pic_num=2,
                  uid="0f3a9c2e-7b11-4c22-9d33-444455556666"):
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>%(ip)s</ipAddress>
<portNo>80</portNo>
<protocol>HTTP</protocol>
<macAddress>c0:56:e3:aa:bb:cc</macAddress>
<channelID>1</channelID>
<dateTime>2026-10-07T08:15:30-06:00</dateTime>
<activePostCount>1</activePostCount>
<eventType>ANPR</eventType>
<eventState>active</eventState>
<eventDescription>ANPR</eventDescription>
<channelName>Entrada</channelName>
<ANPR>
<licensePlate>%(plate)s</licensePlate>
<line>1</line>
<direction>forward</direction>
<confidenceLevel>41</confidenceLevel>
<plateType>unknown</plateType>
<plateColor>white</plateColor>
<licenseBright>0</licenseBright>
<vehicleType>vehicle</vehicleType>
<detectionBeginTime>2026-10-07T08:15:29-06:00</detectionBeginTime>
<vehicleListName>%(list)s</vehicleListName>
<picNum>%(pic)s</picNum>
<pictureInfoList>
<pictureInfo><fileName>licensePlatePicture.jpg</fileName><type>licensePlatePicture</type><dataType>0</dataType><plateRect><X>820</X><Y>640</Y><width>180</width><height>60</height></plateRect></pictureInfo>
<pictureInfo><fileName>detectionPicture.jpg</fileName><type>detectionPicture</type><dataType>0</dataType></pictureInfo>
</pictureInfoList>
</ANPR>
<UUID>%(uid)s</UUID>
</EventNotificationAlert>
""" % {"ip": CAMERA_IP, "plate": plate, "list": vehicle_list, "pic": pic_num,
       "uid": uid}).encode("utf-8")


def hikvision_body(xml, images=((b"licensePlatePicture.jpg", LICENSE_JPG),
                                (b"detectionPicture.jpg", DETECTION_JPG)),
                   with_filename=True, boundary=b"boundary"):
    """El cuerpo tal cual lo arma la camara: CRLF, Content-Length por parte,
    charset entre comillas en el XML, boundary literal `boundary`."""
    def part(name, content_type, payload):
        disposition = b'Content-Disposition: form-data; name="' + name + b'"'
        if with_filename:
            disposition += b'; filename="' + name + b'"'
        return (b"--" + boundary + b"\r\n" + disposition + b"\r\n"
                + b"Content-Type: " + content_type + b"\r\n"
                + b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                + payload + b"\r\n")
    body = part(b"anpr.xml", b'application/xml; charset="UTF-8"', xml)
    for name, data in images:
        body += part(name, b"image/jpeg", data)
    return body + b"--" + boundary + b"--\r\n"


class CameraPayloadTests(unittest.TestCase):
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
        self.settings = Settings({"anpr": {"enabled": True}}, base / "config-runtime.json")
        self.events = AnprEventStore(base / "events.db")
        self.captures = AnprCaptureStore(base / "caps", retention_days=15, max_mb=50)
        app = api_module.create_app(self.settings, gates=mock.Mock(), hikvision_service=None,
                                    screen=mock.Mock(), anpr_store=self.events,
                                    anpr_captures=self.captures)
        app.testing = True
        self.client = app.test_client()

    def post_raw(self, body, remote_addr=CAMERA_IP, headers=None,
                 content_type="multipart/form-data; boundary=boundary"):
        return self.client.post("/anpr-event", data=body, content_type=content_type,
                                headers=headers or {},
                                environ_base={"REMOTE_ADDR": remote_addr})

    def assert_photos_saved(self):
        records, total = self.captures.list()
        self.assertEqual(total, 1)
        rec = records[0]
        self.assertIsNone(rec["licensePlate"])
        self.assertEqual(rec["reason"], "unreadable")
        self.assertEqual(rec["vehicleList"], "otherlist")
        self.assertEqual(rec["capturedAt"], "2026-10-07T08:15:30-06:00")
        self.assertEqual(rec["cameraName"], "Entrada")
        self.assertEqual(rec["sourceIp"], CAMERA_IP)
        self.assertEqual(rec["picturesDeclared"], 2)
        self.assertEqual(rec["eventUid"], "0f3a9c2e-7b11-4c22-9d33-444455556666")
        self.assertEqual(len(rec["images"]), 2)
        self.assertTrue(rec["images"][0].endswith("_0-licenseplatepicture.jpg"), rec["images"])
        self.assertTrue(rec["images"][1].endswith("_1-detectionpicture.jpg"), rec["images"])
        # Los JPG llegan byte a byte identicos
        self.assertEqual((self.captures.dir / rec["images"][0]).read_bytes(), LICENSE_JPG)
        self.assertEqual((self.captures.dir / rec["images"][1]).read_bytes(), DETECTION_JPG)
        # ...y se sirven igual por el API
        resp = self.client.get("/anpr-captures/" + rec["images"][1])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, DETECTION_JPG)
        return rec

    def test_multipart_hikvision_con_filename(self):
        resp = self.post_raw(hikvision_body(hikvision_xml()))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.get_json(), {"ignored": "placa ilegible"})
        self.assert_photos_saved()

    def test_multipart_hikvision_sin_filename(self):
        # Werkzeug mete estas partes en request.form como texto: la foto
        # tiene que salir del respaldo crudo, intacta.
        resp = self.post_raw(hikvision_body(hikvision_xml(), with_filename=False))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.get_json(), {"ignored": "placa ilegible"})
        self.assert_photos_saved()

    def test_a_traves_de_nginx(self):
        # Por el puerto 80 nginx reenvia con X-Forwarded-For = IP real de la
        # camara y remote_addr 127.0.0.1 (ver scripts/nginx).
        resp = self.post_raw(hikvision_body(hikvision_xml()), remote_addr="127.0.0.1",
                             headers={"X-Forwarded-For": CAMERA_IP})
        self.assertEqual(resp.status_code, 200)
        self.assert_photos_saved()

    def test_ip_publica_se_rechaza_sin_guardar_nada(self):
        resp = self.post_raw(hikvision_body(hikvision_xml()), remote_addr="8.8.8.8")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.captures.list(), ([], 0))

    def test_placa_no_autorizada_con_foto_y_fila_descartada(self):
        resp = self.post_raw(hikvision_body(hikvision_xml("BCD456", "blackList")))
        self.assertEqual(resp.get_json(), {"ignored": "no autorizada", "vehicleList": "blacklist"})
        rec = self.captures.list()[0][0]
        self.assertEqual((rec["licensePlate"], rec["reason"], len(rec["images"])),
                         ("BCD456", "not_authorized", 2))
        self.assertEqual(self.events.stats()["discardedStored"], 1)

    def test_placa_autorizada_sin_filename_igual_se_encola(self):
        # El XML tambien se extrae del respaldo crudo cuando no hay filename
        resp = self.post_raw(hikvision_body(hikvision_xml("SJB123", "allowList"), with_filename=False))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.get_json()["queued"])
        self.assertEqual(self.captures.list(), ([], 0))
        self.assertEqual(self.events.stats()["pending"], 1)

    def test_reintento_de_la_camara_no_duplica_fotos(self):
        body = hikvision_body(hikvision_xml())
        self.post_raw(body)
        self.post_raw(body)
        self.assertEqual(self.captures.list()[1], 1)
        self.assertEqual(len(list(self.captures.dir.glob("*.jpg"))), 2)

    def test_varias_placas_con_uuid_de_prefijo_fijo_se_guardan_todas(self):
        # Lo reportado en campo: placas distintas, una tras otra, y la
        # bitacora solo mostraba la primera. La camara no numera sus eventos
        # con UUID aleatorios: solo cambia el final, y la dedupe comparaba el
        # principio. Cada lectura tiene que quedar con sus dos fotos.
        plates = ["BCD456", "KLM789", "unknown", "TRP456", "unknown"]
        for i, plate in enumerate(plates):
            uid = "0f3a9c2e-7b11-4c22-9d33-%012d" % (100 + i)
            resp = self.post_raw(hikvision_body(hikvision_xml(plate, "otherList", uid=uid)))
            self.assertEqual(resp.status_code, 200, resp.data)
        records, total = self.captures.list()
        self.assertEqual(total, 5)
        self.assertEqual(sorted(r["licensePlate"] or "" for r in records),
                         sorted(p if p != "unknown" else "" for p in plates))
        self.assertTrue(all(len(r["images"]) == 2 for r in records))
        self.assertEqual(len(list(self.captures.dir.glob("*.jpg"))), 10)

    def test_xml_declara_fotos_que_no_llegaron_avisa_en_el_log(self):
        # La camara dice picNum=2 pero el multipart solo trae el XML (alarm
        # server sin "picture"): se guarda el registro sin fotos y queda el
        # aviso para diagnosticar en sitio.
        body = hikvision_body(hikvision_xml(pic_num=2), images=())
        # Otros modulos de tests apagan el logging global (logging.disable);
        # assertLogs necesita verlo encendido durante este test.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        with self.assertLogs("aditum.api", level="WARNING") as logs:
            resp = self.post_raw(body)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any("declara 2 foto(s)" in line for line in logs.output), logs.output)
        rec = self.captures.list()[0][0]
        self.assertEqual((rec["images"], rec["picturesDeclared"]), ([], 2))

    def test_xml_crudo_sin_multipart(self):
        resp = self.post_raw(hikvision_xml(pic_num=0), content_type="application/xml")
        self.assertEqual(resp.get_json(), {"ignored": "placa ilegible"})
        rec = self.captures.list()[0][0]
        self.assertEqual((rec["images"], rec["picturesDeclared"]), ([], 0))

    def test_multipart_de_otro_boundary_y_partes_repetidas(self):
        # Boundary largo tipo navegador y dos partes con el mismo name: ambas
        # fotos se guardan (items(multi=True)).
        xml = hikvision_xml()
        boundary = b"----WebKitFormBoundary7MA4YWxkTrZu0gW"
        body = hikvision_body(xml, images=((b"picture.jpg", LICENSE_JPG), (b"picture.jpg", DETECTION_JPG)),
                              boundary=boundary)
        resp = self.post_raw(body, content_type="multipart/form-data; boundary=" + boundary.decode())
        self.assertEqual(resp.status_code, 200)
        rec = self.captures.list()[0][0]
        self.assertEqual(len(rec["images"]), 2)
        self.assertEqual((self.captures.dir / rec["images"][0]).read_bytes(), LICENSE_JPG)
        self.assertEqual((self.captures.dir / rec["images"][1]).read_bytes(), DETECTION_JPG)
        self.assertEqual(json.loads((self.captures.dir / rec["name"]).read_text())["images"], rec["images"])


if __name__ == "__main__":
    unittest.main()
