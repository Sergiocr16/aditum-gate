"""Control de relays (portones) por GPIO.

Los pines vienen de la config (gpio.gates); el default en config-default.json
reproduce los 10 relays historicos en modo BOARD.
"""
import logging
import threading
import time

log = logging.getLogger("aditum.gpio")

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None
    log.warning("RPi.GPIO no disponible: GPIO en modo simulado (solo desarrollo)")


def _levels(gate):
    """(reposo, activo) segun tipo de rele: NO = HIGH/LOW, NC = LOW/HIGH."""
    if gate["normallyOpen"]:
        return GPIO.HIGH, GPIO.LOW
    return GPIO.LOW, GPIO.HIGH


class GateController:
    def __init__(self, settings):
        self.pulse_seconds = settings.pulse_seconds
        self.gates = {g["id"]: {"id": g["id"], "pin": g["pin"],
                                "normallyOpen": g.get("normallyOpen", True),
                                "status": 0}
                      for g in settings.gates}
        self._lock = threading.Lock()

        if GPIO:
            GPIO.setmode(GPIO.BOARD if settings.gpio_mode == "BOARD" else GPIO.BCM)
            GPIO.setwarnings(False)
            # Un pin invalido (3.3V/GND/fuera de rango) no debe tumbar el
            # proceso completo: se excluye ese porton y el resto sigue.
            for gate in list(self.gates.values()):
                try:
                    # initial= deja el rele en reposo desde el setup, sin
                    # glitch de nivel al boot (importante en gates NC). Entre
                    # power-on y este arranque el pin queda en el estado de
                    # reset del SoC; eso solo se mitiga con pull externo.
                    GPIO.setup(gate["pin"], GPIO.OUT, initial=_levels(gate)[0])
                except (ValueError, RuntimeError) as e:
                    log.error("Porton %s con pin invalido (%s): %s — excluido",
                              gate["id"], gate["pin"], e)
                    del self.gates[gate["id"]]

    def _gate(self, gate_id):
        gate = self.gates.get(gate_id)
        if gate is None:
            raise KeyError(f"Porton {gate_id} no existe en la configuracion")
        return gate

    def open_gate(self, gate_id):
        """Pulso de apertura: nivel activo durante pulse_seconds y de vuelta a reposo."""
        gate = self._gate(gate_id)
        with self._lock:
            gate["status"] = 1
            if GPIO:
                idle, active = _levels(gate)
                GPIO.output(gate["pin"], active)
                time.sleep(self.pulse_seconds)
                GPIO.output(gate["pin"], idle)
            gate["status"] = 0
        log.info("Porton %s abierto (pulso %ss)", gate_id, self.pulse_seconds)
        return gate

    def close_gate(self, gate_id):
        gate = self._gate(gate_id)
        with self._lock:
            gate["status"] = 0
            if GPIO:
                GPIO.output(gate["pin"], _levels(gate)[0])  # rele a reposo
        log.info("Porton %s cerrado", gate_id)
        return gate

    def pin_value(self, gate_id):
        gate = self._gate(gate_id)
        if GPIO:
            return GPIO.input(gate["pin"])
        return 1

    def status_all(self):
        return list(self.gates.values())
