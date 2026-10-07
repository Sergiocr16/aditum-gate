"""Tests de la bitacora local de placas no reconocidas (anpr_captures) y de
su integracion con POST /anpr-event.

Correr desde la raiz del repo (sin red):
    python3 -m unittest discover -s device/tests -t device -v
"""
import io
import json
import logging
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from aditum_gate import admin_auth, anpr_captures
from aditum_gate import api as api_module
from aditum_gate import settings as settings_module
from aditum_gate.anpr_captures import (REASON_NOT_AUTHORIZED, REASON_UNREADABLE,
                                       STAMP_FORMAT, AnprCaptureStore)
from aditum_gate.anpr_events import AnprEventStore, parse_event_xml
from aditum_gate.settings import Settings

logging.disable(logging.CRITICAL)

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"


def event_xml(plate, vehicle_list="otherList", uid="0f3a9c2e-1111-2222-3333-444455556666"):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <ipAddress>192.168.100.106</ipAddress>
  <dateTime>2026-10-07T08:15:30-06:00</dateTime>
  <eventType>ANPR</eventType>
  <channelName>Entrada</channelName>
  <UUID>{uid}</UUID>
  <ANPR>
    <licensePlate>{plate}</licensePlate>
    <confidenceLevel>87</confidenceLevel>
    <vehicleListName>{vehicle_list}</vehicleListName>
  </ANPR>
</EventNotificationAlert>""".encode()


def make_event(plate="ABC123", vehicle_list="otherlist", uid="0f3a9c2e-aaaa"):
    return {"eventUid": uid, "licensePlate": plate, "capturedAt": "2026-10-07T08:15:30-06:00",
            "confidenceLevel": 87, "cameraName": "Entrada", "vehicleList": vehicle_list}


class ParseUnreadableTests(unittest.TestCase):
    def test_placa_unknown_devuelve_evento_sin_placa(self):
        event = parse_event_xml(event_xml("unknown"))
        self.assertIsNotNone(event)
        self.assertIsNone(event["licensePlate"])
        self.assertEqual(event["vehicleList"], "otherlist")

    def test_placa_legible_sigue_igual(self):
        event = parse_event_xml(event_xml("SJB123", "allowList"))
        self.assertEqual(event["licensePlate"], "SJB123")
        self.assertEqual(event["vehicleList"], "allowlist")

    def test_heartbeat_sigue_siendo_none(self):
        self.assertIsNone(parse_event_xml(
            b"<EventNotificationAlert><eventType>videoloss</eventType></EventNotificationAlert>"))


class CaptureStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AnprCaptureStore(Path(self.tmp.name) / "caps", retention_days=15, max_mb=10)

    def test_guarda_json_y_fotos_con_el_mismo_prefijo(self):
        rec = self.store.save(make_event(), [("licensePlatePicture.jpg", JPG),
                                             ("detectionPicture.jpg", JPG)],
                              source_ip="192.168.100.106", reason=REASON_NOT_AUTHORIZED)
        self.assertIsNotNone(rec)
        self.assertTrue(rec["name"].endswith(
            "_ABC123_%s.json" % anpr_captures._uid_for_name("0f3a9c2e-aaaa")))
        self.assertEqual(len(rec["images"]), 2)
        self.assertTrue(rec["images"][0].endswith("_0-licenseplatepicture.jpg"))
        self.assertTrue(rec["images"][1].endswith("_1-detectionpicture.jpg"))
        files = sorted(os.listdir(self.store.dir))
        self.assertEqual(len(files), 3)
        for name in rec["images"]:
            self.assertEqual((self.store.dir / name).read_bytes(), JPG)
        saved = json.loads((self.store.dir / rec["name"]).read_text())
        self.assertEqual(saved["licensePlate"], "ABC123")
        self.assertEqual(saved["capturedAt"], "2026-10-07T08:15:30-06:00")
        self.assertEqual(saved["reason"], REASON_NOT_AUTHORIZED)
        self.assertEqual(saved["sourceIp"], "192.168.100.106")

    def test_sin_placa_usa_etiqueta_y_guarda_igual(self):
        rec = self.store.save(make_event(plate=None), [("detectionPicture.jpg", JPG)],
                              reason=REASON_UNREADABLE)
        self.assertIn("_SIN-PLACA_", rec["name"])
        self.assertIsNone(rec["licensePlate"])
        self.assertEqual(rec["reason"], REASON_UNREADABLE)

    def test_sin_fotos_guarda_solo_el_json(self):
        rec = self.store.save(make_event(), [])
        self.assertEqual(rec["images"], [])
        self.assertEqual(os.listdir(self.store.dir), [rec["name"]])

    def test_reintento_de_la_camara_no_duplica(self):
        self.store.save(make_event(), [("a.jpg", JPG)])
        # Otros modulos de tests apagan el logging global (logging.disable);
        # assertLogs necesita verlo encendido durante este test.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        with self.assertLogs("aditum.anpr.captures", level="INFO") as logs:
            self.assertIsNone(self.store.save(make_event(), [("a.jpg", JPG)]))
        self.assertIn("repetida", logs.output[0])
        self.assertEqual(len(os.listdir(self.store.dir)), 2)

    def test_uuids_con_el_mismo_prefijo_son_lecturas_distintas(self):
        # Lo que paso en campo: la camara numera sus eventos con un prefijo
        # fijo y solo se guardaba la primera lectura. Placas distintas y
        # UUID distintos tienen que dar registros distintos, aunque los
        # UUID solo cambien al final.
        uids = ["0f3a9c2e-0000-0000-0000-00000000000%d" % i for i in range(1, 6)]
        for i, uid in enumerate(uids):
            rec = self.store.save(make_event(plate="PLACA%d" % i, uid=uid), [("a.jpg", JPG)])
            self.assertIsNotNone(rec, uid)
            self.assertRegex(rec["name"], anpr_captures.SAFE_NAME)
        self.assertEqual(self.store.list()[1], 5)
        self.assertEqual(self.store.stats()["count"], 5)
        # Y el mismo UUID completo si es un reintento
        self.assertIsNone(self.store.save(make_event(plate="OTRA", uid=uids[2]), []))
        self.assertEqual(self.store.list()[1], 5)

    def test_placa_rara_no_rompe_el_nombre(self):
        rec = self.store.save(make_event(plate="ab/..c 12", uid="deadbeef"), [])
        self.assertIn("_ABC12_%s.json" % anpr_captures._uid_for_name("deadbeef"), rec["name"])
        self.assertEqual(self.store.list()[0][0]["licensePlate"], "ab/..c 12")

    def test_uuid_no_hex_de_la_camara_igual_produce_nombre_valido(self):
        rec = self.store.save(make_event(uid="EV-XYZ_42"), [("a.jpg", JPG)])
        self.assertIsNotNone(rec)
        match = anpr_captures.SAFE_NAME.match(rec["name"])
        self.assertEqual((match["plate"], len(match["uid"])), ("ABC123", 12))
        self.assertEqual(match["uid"], anpr_captures._uid_for_name("ev-xyz_42"))
        self.assertEqual(self.store.list()[1], 1)
        self.assertIsNotNone(self.store.file_path(rec["images"][0]))
        self.assertEqual(self.store.clear(), 1)

    def test_list_devuelve_del_mas_nuevo_al_mas_viejo(self):
        self.store.save(make_event(plate="AAA111", uid="11111111"), [])
        # Mismo segundo: el nombre lleva la placa, asi que se fuerza un
        # stamp anterior para el primero y se comprueba el orden.
        old = self.store.dir / ("20200101-000000_OLD999_22222222.json")
        old.write_text(json.dumps({"name": old.name, "licensePlate": "OLD999", "images": []}))
        records, total = self.store.list()
        self.assertEqual([r["licensePlate"] for r in records], ["AAA111", "OLD999"])
        self.assertEqual(total, 2)
        page, total = self.store.list(limit=1, offset=1)
        self.assertEqual(([r["licensePlate"] for r in page], total), (["OLD999"], 2))

    def test_filtros_por_fecha_hora_placa_y_motivo(self):
        self._write_dated(datetime(2026, 10, 6, 7, 30, 0), "SJB123", "aaaaaaaa")
        self._write_dated(datetime(2026, 10, 7, 8, 15, 30), "SIN-PLACA", "bbbbbbbb")
        self._write_dated(datetime(2026, 10, 7, 18, 45, 10), "XYZ789", "cccccccc")
        self._write_dated(datetime(2026, 10, 7, 23, 59, 59), "SJB999", "dddddddd")

        def plates(**f):
            recs, total = self.store.list(**f)
            return [r["licensePlate"] for r in recs], total

        self.assertEqual(plates(date="20261007")[1], 3)
        self.assertEqual(plates(date="20261006"), (["SJB123"], 1))
        self.assertEqual(plates(date="20261007", time_from="080000", time_to="190000"),
                         (["XYZ789", "SIN-PLACA"], 2))
        self.assertEqual(plates(time_to="075959"), (["SJB123"], 1))
        self.assertEqual(plates(time_from="235959"), (["SJB999"], 1))
        self.assertEqual(plates(plate="SJB")[1], 2)
        self.assertEqual(plates(plate="SJB", date="20261007"), (["SJB999"], 1))
        self.assertEqual(plates(reason=REASON_UNREADABLE), (["SIN-PLACA"], 1))
        self.assertEqual(plates(reason=REASON_NOT_AUTHORIZED)[1], 3)
        self.assertEqual(plates(date="20260101"), ([], 0))
        self.assertEqual(self.store.days(), {"2026-10-07": 3, "2026-10-06": 1})

    def test_file_path_rechaza_nombres_ajenos(self):
        rec = self.store.save(make_event(), [("a.jpg", JPG)])
        self.assertIsNotNone(self.store.file_path(rec["images"][0]))
        self.assertIsNotNone(self.store.file_path(rec["name"]))
        for bad in ("../device-token.txt", "device-token.txt", "", None,
                    "20260101-000000_ABC_12345678.png", rec["name"] + "/../x"):
            self.assertIsNone(self.store.file_path(bad), bad)

    def _write_dated(self, when, plate, uid, image_bytes=b""):
        base = "%s_%s_%s" % (when.strftime(STAMP_FORMAT), plate, uid)
        images = []
        if image_bytes:
            images.append(base + "_0-detectionpicture.jpg")
            (self.store.dir / images[0]).write_bytes(image_bytes)
        (self.store.dir / (base + ".json")).write_text(json.dumps(
            {"name": base + ".json", "licensePlate": plate, "images": images}))
        return base

    def test_purga_por_edad_borra_json_y_fotos(self):
        # uid de 8 hex: el formato de los registros que ya hay en la flota
        now = datetime.now()
        old = self._write_dated(now - timedelta(days=16), "OLD111", "aaaaaaaa", JPG)
        fresh = self._write_dated(now - timedelta(days=14), "NEW222", "bbbbbbbb", JPG)
        self.assertEqual(self.store.purge(), 1)
        names = os.listdir(self.store.dir)
        self.assertFalse(any(n.startswith(old) for n in names))
        self.assertEqual(sum(1 for n in names if n.startswith(fresh)), 2)

    def test_purga_por_tamano_borra_lo_mas_viejo_primero(self):
        store = AnprCaptureStore(Path(self.tmp.name) / "caps2", retention_days=15, max_mb=1)
        self.store = store
        now = datetime.now()
        big = b"x" * (400 * 1024)
        oldest = self._write_dated(now - timedelta(hours=3), "P1", "11111111", big)
        middle = self._write_dated(now - timedelta(hours=2), "P2", "22222222", big)
        newest = self._write_dated(now - timedelta(hours=1), "P3", "33333333", big)
        # 1.2 MB > 1 MB: sale el mas viejo y queda en 0.8 MB
        self.assertEqual(store.purge(), 1)
        names = os.listdir(store.dir)
        self.assertFalse(any(n.startswith(oldest) for n in names))
        self.assertTrue(any(n.startswith(middle) for n in names))
        self.assertTrue(any(n.startswith(newest) for n in names))
        self.assertEqual(store.stats()["count"], 2)

    def test_disco_casi_lleno_guarda_el_registro_sin_fotos(self):
        with mock.patch.object(anpr_captures.shutil, "disk_usage",
                               return_value=mock.Mock(free=200 * 1024 * 1024)):
            rec = self.store.save(make_event(uid="11111111"), [("a.jpg", JPG)])
        self.assertEqual(rec["images"], [])
        self.assertEqual(rec["imagesSkipped"], "disco lleno")
        self.assertEqual(os.listdir(self.store.dir), [rec["name"]])
        # Con espacio, las fotos vuelven a guardarse
        rec = self.store.save(make_event(uid="22222222"), [("a.jpg", JPG)])
        self.assertEqual(len(rec["images"]), 1)
        self.assertNotIn("imagesSkipped", rec)
        self.assertGreater(self.store.stats()["diskFreeMb"], 0)

    def test_clear_borra_todo(self):
        self.store.save(make_event(uid="11111111"), [("a.jpg", JPG)])
        self.store.save(make_event(uid="22222222"), [("a.jpg", JPG)])
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual(os.listdir(self.store.dir), [])
        stats = self.store.stats()
        stats.pop("diskFreeMb")
        self.assertEqual(stats, {"count": 0, "bytes": 0, "retentionDays": 15,
                                 "maxMb": 10, "minFreeMb": anpr_captures.MIN_FREE_MB})


class AnprEventEndpointTests(unittest.TestCase):
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
        self.settings = Settings({"anpr": {"enabled": True}}, "test")
        self.events = AnprEventStore(base / "events.db")
        self.captures = AnprCaptureStore(base / "caps", retention_days=15, max_mb=10)
        app = api_module.create_app(self.settings, gates=mock.Mock(), hikvision_service=None,
                                    screen=mock.Mock(), anpr_store=self.events,
                                    anpr_captures=self.captures)
        app.testing = True
        self.client = app.test_client()

    def post_event(self, xml, images=("licensePlatePicture.jpg", "detectionPicture.jpg")):
        data = {"anpr.xml": (io.BytesIO(xml), "anpr.xml", "application/xml")}
        for name in images:
            data[name] = (io.BytesIO(JPG), name, "image/jpeg")
        return self.client.post("/anpr-event", data=data, content_type="multipart/form-data",
                                environ_base={"REMOTE_ADDR": "192.168.100.106"})

    def test_placa_fuera_del_allowlist_se_guarda_con_fotos(self):
        resp = self.post_event(event_xml("XYZ789", "otherList"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["ignored"], "no autorizada")
        caps, _ = self.captures.list()
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["licensePlate"], "XYZ789")
        self.assertEqual(caps[0]["reason"], REASON_NOT_AUTHORIZED)
        self.assertEqual(caps[0]["sourceIp"], "192.168.100.106")
        self.assertEqual(caps[0]["capturedAt"], "2026-10-07T08:15:30-06:00")
        self.assertEqual(len(caps[0]["images"]), 2)
        # ...y sigue quedando como descartada en la cola, como antes
        self.assertEqual(self.events.stats()["discardedStored"], 1)

    def test_placa_ilegible_se_guarda_y_no_se_encola(self):
        resp = self.post_event(event_xml("unknown", "otherList"))
        self.assertEqual(resp.get_json(), {"ignored": "placa ilegible"})
        caps, _ = self.captures.list()
        self.assertEqual(len(caps), 1)
        self.assertIsNone(caps[0]["licensePlate"])
        self.assertEqual(caps[0]["reason"], REASON_UNREADABLE)
        self.assertEqual(len(caps[0]["images"]), 2)
        stats = self.events.stats()
        self.assertEqual((stats["pending"], stats["discardedStored"]), (0, 0))

    def test_placa_autorizada_no_se_guarda(self):
        resp = self.post_event(event_xml("SJB123", "allowList"))
        self.assertTrue(resp.get_json()["queued"])
        self.assertEqual(self.captures.list(), ([], 0))
        self.assertEqual(self.events.stats()["pending"], 1)

    def test_con_filtro_apagado_igual_se_guarda_la_no_autorizada(self):
        self.settings.anpr_only_authorized = False
        resp = self.post_event(event_xml("XYZ789", "blackList"))
        self.assertTrue(resp.get_json()["queued"])
        self.assertEqual(self.captures.list()[1], 1)

    def test_xml_crudo_sin_fotos_guarda_solo_el_registro(self):
        resp = self.client.post("/anpr-event", data=event_xml("unknown"),
                                content_type="application/xml",
                                environ_base={"REMOTE_ADDR": "192.168.100.106"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.captures.list()[0][0]["images"], [])

    def test_fallo_del_disco_no_tumba_el_evento(self):
        with mock.patch.object(self.captures, "save", side_effect=OSError("disco lleno")):
            resp = self.post_event(event_xml("XYZ789", "otherList"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.events.stats()["discardedStored"], 1)

    def test_endpoints_de_la_bitacora(self):
        self.post_event(event_xml("XYZ789", "otherList"))
        resp = self.client.get("/anpr-captures")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["retentionDays"], 15)
        self.assertEqual(list(body["days"].values()), [1])
        self.assertEqual(body["filters"], {})
        # Filtros por query string: lo invalido se ignora, lo valido filtra
        today = datetime.now().strftime("%Y-%m-%d")
        resp = self.client.get("/anpr-captures?date=%s&from=00:00&to=23:59&plate=xyz&reason=not_authorized" % today)
        self.assertEqual(resp.get_json()["total"], 1)
        self.assertEqual(resp.get_json()["filters"],
                         {"date": today.replace("-", ""), "time_from": "000000",
                          "time_to": "235959", "plate": "XYZ", "reason": "not_authorized"})
        self.assertEqual(self.client.get("/anpr-captures?date=2020-01-01").get_json()["total"], 0)
        self.assertEqual(self.client.get("/anpr-captures?reason=unreadable").get_json()["total"], 0)
        self.assertEqual(self.client.get("/anpr-captures?plate=ABC").get_json()["total"], 0)
        resp = self.client.get("/anpr-captures?date=ayer&from=25h&limit=x&reason=otro")
        self.assertEqual((resp.get_json()["total"], resp.get_json()["filters"]), (1, {}))
        image = body["captures"][0]["images"][0]
        resp = self.client.get("/anpr-captures/" + image)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, JPG)
        self.assertEqual(resp.mimetype, "image/jpeg")
        resp = self.client.get("/anpr-captures/" + body["captures"][0]["name"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/anpr-captures/device-token.txt").status_code, 404)
        self.assertEqual(self.client.get("/anpr-captures/..%2Fdevice-token.txt").status_code, 404)
        resp = self.client.delete("/anpr-captures")
        self.assertEqual(resp.get_json(), {"deleted": 1})
        self.assertEqual(self.client.get("/anpr-captures").get_json()["count"], 0)

    def test_sin_bitacora_responde_404(self):
        app = api_module.create_app(self.settings, gates=mock.Mock(), hikvision_service=None,
                                    screen=mock.Mock(), anpr_store=self.events)
        app.testing = True
        client = app.test_client()
        self.assertEqual(client.get("/anpr-captures").status_code, 404)
        # ...pero el evento se procesa igual que antes
        resp = client.post("/anpr-event", data=event_xml("unknown"), content_type="application/xml",
                           environ_base={"REMOTE_ADDR": "192.168.100.106"})
        self.assertEqual(resp.get_json(), {"ignored": "placa ilegible"})


if __name__ == "__main__":
    unittest.main()
