"""Portones relacionados (gpio.gates[].linkedGates) en GateController.

Corre en modo simulado (sin RPi.GPIO): los pulsos no tocan hardware y no
duermen, asi que los delays se prueban cortos y reales."""
import logging
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from aditum_gate.gpio_relays import GateController


def _settings(gates, pulse=1):
    return SimpleNamespace(gates=gates, pulse_seconds=pulse, gpio_mode="BOARD")


def _wait(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class LinkedGatesTest(unittest.TestCase):
    def test_abrir_el_principal_pulsa_los_relacionados_tras_su_delay(self):
        ctl = GateController(_settings([
            {"id": 1, "pin": 7, "linkedGates": [{"gateId": 2, "delaySeconds": 0.05},
                                                {"gateId": 3, "delaySeconds": 0.15}]},
            {"id": 2, "pin": 11},
            {"id": 3, "pin": 12},
        ]))
        pulsed = []
        with mock.patch.object(ctl, "_pulse", side_effect=lambda g: pulsed.append((g["id"], time.monotonic()))):
            t0 = time.monotonic()
            gate = ctl.open_gate(1)
            self.assertEqual(gate["id"], 1)
            # La respuesta no espera a los relacionados
            self.assertEqual([g for g, _ in pulsed], [1])
            self.assertTrue(_wait(lambda: len(pulsed) == 3))
        self.assertEqual([g for g, _ in pulsed], [1, 2, 3])
        self.assertGreaterEqual(pulsed[1][1] - t0, 0.05)
        self.assertGreaterEqual(pulsed[2][1] - t0, 0.15)

    def test_los_relacionados_no_encadenan(self):
        # 1 -> 2 y 2 -> 1: abrir 1 pulsa 2 una vez y ahi termina (sin ciclo)
        ctl = GateController(_settings([
            {"id": 1, "pin": 7, "linkedGates": [{"gateId": 2, "delaySeconds": 0}]},
            {"id": 2, "pin": 11, "linkedGates": [{"gateId": 1, "delaySeconds": 0}]},
        ]))
        pulsed = []
        with mock.patch.object(ctl, "_pulse", side_effect=lambda g: pulsed.append(g["id"])):
            ctl.open_gate(1)
            self.assertTrue(_wait(lambda: len(pulsed) >= 2))
            time.sleep(0.1)
        self.assertEqual(pulsed, [1, 2])

    def test_links_a_portones_inexistentes_o_a_si_mismo_se_ignoran(self):
        # Otros modulos de tests apagan el logging global (logging.disable);
        # aca hace falta leerlo
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        with self.assertLogs("aditum.gpio", level="ERROR") as logs:
            ctl = GateController(_settings([
                {"id": 1, "pin": 7, "linkedGates": [{"gateId": 1, "delaySeconds": 1},
                                                    {"gateId": 9, "delaySeconds": 1},
                                                    {"gateId": 2, "delaySeconds": 2}]},
                {"id": 2, "pin": 11},
            ]))
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(ctl.gates[1]["linkedGates"], [{"gateId": 2, "delaySeconds": 2.0}])
        self.assertEqual(ctl.gates[2]["linkedGates"], [])

    def test_gate_status_expone_los_relacionados(self):
        ctl = GateController(_settings([
            {"id": 1, "pin": 7, "linkedGates": [{"gateId": 2, "delaySeconds": 3}]},
            {"id": 2, "pin": 11, "normallyOpen": False},
        ]))
        self.assertEqual(ctl.status_all(), [
            {"id": 1, "pin": 7, "normallyOpen": True, "status": 0,
             "linkedGates": [{"gateId": 2, "delaySeconds": 3.0}]},
            {"id": 2, "pin": 11, "normallyOpen": False, "status": 0, "linkedGates": []},
        ])


if __name__ == "__main__":
    unittest.main()
