"""Vigencia de las filas que el Pi escribe en la lista de placas de la camara.

La camara evalua la vigencia contra SU propio reloj. Si el inicio fuera "hoy"
segun el Pi, una camara con la hora atrasada (zona horaria mal puesta, sin
NTP) reportaria otherList todas las placas recien sincronizadas hasta
alcanzar esa fecha. Estos tests fijan que el inicio es una fecha fija en el
pasado, tanto en el alta suelta (ADD) como en el full sync.

Correr desde la raiz del repo (sin red):
    python3 -m unittest discover -s device/tests -t device -v
"""
import unittest
from datetime import datetime
from unittest import mock

from aditum_gate import anpr
from aditum_gate.anpr import (ALLOW_LIST_GROUP, VALIDITY_START, VALIDITY_YEARS,
                              AnprCameraClient, _COL_END, _COL_GROUP, _COL_NO,
                              _COL_PLATE, _COL_START)

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


if __name__ == "__main__":
    unittest.main()
