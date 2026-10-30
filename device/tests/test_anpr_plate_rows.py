"""Vigencia de las filas que el Pi escribe en la lista de placas de la camara.

La camara evalua la vigencia contra SU propio reloj. Si el inicio fuera "hoy"
segun el Pi, una camara con la hora atrasada (zona horaria mal puesta, sin
NTP) reportaria otherList todas las placas recien sincronizadas hasta
alcanzar esa fecha. Estos tests fijan que el inicio es una fecha fija en el
pasado, tanto en el alta suelta (ADD) como en el full sync.

Correr desde la raiz del repo (sin red):
    python3 -m unittest discover -s device/tests -t device -v
"""
import threading
import unittest
from datetime import datetime
from unittest import mock

from aditum_gate import anpr
from aditum_gate.anpr import (ALLOW_LIST_GROUP, IMPORT_READ_MAX,
                              IMPORT_READ_MIN, ISAPI_SYNC_TIMEOUT,
                              VALIDITY_START, VALIDITY_YEARS, AnprCameraClient,
                              _COL_END, _COL_GROUP, _COL_NO, _COL_PLATE,
                              _COL_START, import_timeout)

HEADER = ["No.", "License Plate", "Group", "Start Date", "End Date", "Card No."]
CAMERA = ("192.168.100.2", "admin", "secreto")


class NewRowTest(unittest.TestCase):

    def test_inicio_fijo_en_el_pasado_no_hoy(self):
        row = AnprCameraClient._new_row("BMT414")
        self.assertEqual(row[_COL_START], VALIDITY_START)
        self.assertLess(row[_COL_START], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(row[_COL_PLATE], "BMT414")
        self.assertEqual(row[_COL_GROUP], ALLOW_LIST_GROUP)

    def test_fin_es_hoy_mas_validity_years(self):
        row = AnprCameraClient._new_row("BMT414")
        end = datetime.strptime(row[_COL_END], "%Y-%m-%d")
        self.assertEqual(end.year, datetime.now().year + VALIDITY_YEARS)
        self.assertGreater(row[_COL_END], row[_COL_START])

    def test_29_feb_cae_a_28_en_anio_destino_no_bisiesto(self):
        # 2024-02-29 + 20 = 2044, bisiesto; se fuerza VALIDITY_YEARS impar
        # para caer en un anio no bisiesto y cubrir el fallback.
        fake_now = datetime(2024, 2, 29, 10, 0, 0)
        with mock.patch.object(anpr, "datetime") as dt, \
                mock.patch.object(anpr, "VALIDITY_YEARS", 21):
            dt.now.return_value = fake_now
            row = AnprCameraClient._new_row("ABC123")
        self.assertEqual(row[_COL_END], "2045-02-28")
        self.assertEqual(row[_COL_START], VALIDITY_START)


class RowsWrittenToCameraTest(unittest.TestCase):
    """ADD y full sync escriben filas con el inicio fijo, y las filas que ya
    estaban conservan sus fechas en el ADD."""

    def setUp(self):
        self.client = AnprCameraClient()
        self.put = mock.patch.object(self.client, "put_plate_rows").start()
        self.addCleanup(mock.patch.stopall)

    def _written_rows(self):
        self.put.assert_called_once()
        return self.put.call_args[0][4]

    def test_add_agrega_fila_con_inicio_fijo_y_conserva_las_existentes(self):
        existing = ["1", "ABL299", "1", "2026-10-08", "2046-10-08", ""]
        with mock.patch.object(self.client, "get_plate_rows",
                               return_value=(HEADER, [list(existing)])):
            detail = self.client.apply_plate(*CAMERA, "ADD", "BMT414")
        self.assertEqual(detail, "created")
        rows = self._written_rows()
        self.assertEqual([r[_COL_PLATE] for r in rows], ["ABL299", "BMT414"])
        self.assertEqual(rows[0][_COL_START], "2026-10-08")  # no se toca
        self.assertEqual(rows[1][_COL_START], VALIDITY_START)
        self.assertEqual([r[_COL_NO] for r in rows], ["1", "2"])

    def test_full_sync_reescribe_todas_con_inicio_fijo(self):
        old = ["1", "ABL299", "1", "2026-10-08", "2046-10-08", ""]
        with mock.patch.object(self.client, "get_plate_rows",
                               return_value=(HEADER, [old])), \
                mock.patch.object(self.client, "plate_capacity",
                                  return_value=10000):
            count = self.client.replace_plates(
                *CAMERA, ["ABL299", "BMT414", "BMT414", ""])
        self.assertEqual(count, 2)
        rows = self._written_rows()
        self.assertEqual([r[_COL_PLATE] for r in rows], ["ABL299", "BMT414"])
        for row in rows:
            self.assertEqual(row[_COL_START], VALIDITY_START)
            self.assertEqual(row[_COL_GROUP], ALLOW_LIST_GROUP)
        self.assertEqual([r[_COL_NO] for r in rows], ["1", "2"])


class ListSizedTimeoutsTest(unittest.TestCase):
    """Export e import escalan con el tamano de la lista: la camara genera el
    CSV completo antes del primer byte (una con 2656 placas tarda 6-7 s). El
    probe ("probar conexion") y el alta suelta usaban el timeout corto de
    2.5 s y fallaban con CAMERA_TIMEOUT contra cualquier lista grande,
    aunque desde el navegador la camara respondiera bien."""

    def setUp(self):
        self.client = AnprCameraClient()
        self.get = mock.patch.object(self.client, "get_plate_rows",
                                     return_value=(HEADER, [])).start()
        self.put = mock.patch.object(self.client, "put_plate_rows").start()
        mock.patch.object(self.client, "plate_capacity",
                          return_value=10000).start()
        self.addCleanup(mock.patch.stopall)

    def test_probe_exporta_con_el_timeout_largo(self):
        result = self.client.probe(*CAMERA)
        self.assertEqual(result["plates"], 0)
        self.assertEqual(self.get.call_args.kwargs["timeout"], ISAPI_SYNC_TIMEOUT)

    def test_alta_suelta_exporta_e_importa_con_los_timeouts_largos(self):
        self.client.apply_plate(*CAMERA, "ADD", "BMT414")
        self.assertEqual(self.get.call_args.kwargs["timeout"], ISAPI_SYNC_TIMEOUT)
        self.assertEqual(self.put.call_args.kwargs["timeout"], import_timeout([1]))

    def test_import_escala_con_las_filas(self):
        # Un sync de 2655 placas no entraba en 12 s fijos (CAMERA_TIMEOUT real
        # en produccion): con 10 ms/placa quedan ~30 s.
        self.assertEqual(import_timeout([])[1], IMPORT_READ_MIN)
        self.assertEqual(import_timeout([0] * 2655)[1], 4 + 26.55)
        self.assertEqual(import_timeout([0] * 50000)[1], IMPORT_READ_MAX)
        self.assertEqual(import_timeout([0] * 2655)[0], 3)

    def test_full_sync_importa_con_timeout_segun_la_lista(self):
        plates = [f"P{i:04d}" for i in range(2655)]
        self.client.replace_plates(*CAMERA, plates)
        self.assertEqual(self.put.call_args.kwargs["timeout"],
                         import_timeout(plates))


class PerCameraLockTest(unittest.TestCase):
    """El import REEMPLAZA la lista: dos read-modify-write solapados contra la
    misma camara (el backend reintenta mientras el primero sigue exportando)
    se pisarian y una placa se perderia. Se serializan por IP."""

    def test_dos_altas_concurrentes_no_pierden_ninguna_placa(self):
        client = AnprCameraClient()
        camera_rows = []  # "la lista que tiene la camara"
        # Los dos hilos se esperan DENTRO del export: sin lock ambos leen la
        # lista vieja (vacia) y el segundo import pisa al primero. Con lock el
        # segundo nunca llega mientras el primero exporta, la barrera vence y
        # cada uno sigue solo.
        both_exporting = threading.Barrier(2)

        def slow_export(ip, user, password, timeout=None):
            rows = [list(r) for r in camera_rows]
            try:
                both_exporting.wait(0.2)
            except threading.BrokenBarrierError:
                pass
            return HEADER, rows

        def do_import(ip, user, password, header, rows, timeout=None):
            camera_rows[:] = rows

        with mock.patch.object(client, "get_plate_rows", side_effect=slow_export), \
                mock.patch.object(client, "put_plate_rows", side_effect=do_import):
            threads = [threading.Thread(target=client.apply_plate,
                                        args=(*CAMERA, "ADD", plate))
                       for plate in ("AAA111", "BBB222")]
            for th in threads:
                th.start()
            for th in threads:
                th.join(5)
        self.assertEqual(sorted(r[_COL_PLATE] for r in camera_rows),
                         ["AAA111", "BBB222"])

    def test_camaras_distintas_no_comparten_lock(self):
        client = AnprCameraClient()
        self.assertIs(client._camera_lock("10.0.0.1"), client._camera_lock("10.0.0.1"))
        self.assertIsNot(client._camera_lock("10.0.0.1"), client._camera_lock("10.0.0.2"))


if __name__ == "__main__":
    unittest.main()
