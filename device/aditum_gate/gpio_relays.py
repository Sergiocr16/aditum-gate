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


class GateController:
    def __init__(self, settings):
        self.pulse_seconds = settings.pulse_seconds
        self.gates = {g["id"]: {"id": g["id"], "pin": g["pin"], "status": 0}
                      for g in settings.gates}
        self._lock = threading.Lock()

        if GPIO:
            GPIO.setmode(GPIO.BOARD if settings.gpio_mode == "BOARD" else GPIO.BCM)
            GPIO.setwarnings(False)
            for gate in self.gates.values():
                GPIO.setup(gate["pin"], GPIO.OUT)
                GPIO.output(gate["pin"], GPIO.HIGH)  # relay inactivo

    def _gate(self, gate_id):
        gate = self.gates.get(gate_id)
        if gate is None:
            raise KeyError(f"Porton {gate_id} no existe en la configuracion")
        return gate

    def open_gate(self, gate_id):
        """Pulso de apertura: LOW durante pulse_seconds y de vuelta a HIGH."""
        gate = self._gate(gate_id)
        with self._lock:
            gate["status"] = 1
            if GPIO:
                GPIO.output(gate["pin"], GPIO.LOW)
                time.sleep(self.pulse_seconds)
                GPIO.output(gate["pin"], GPIO.HIGH)
            gate["status"] = 0
        log.info("Porton %s abierto (pulso %ss)", gate_id, self.pulse_seconds)
        return gate

    def close_gate(self, gate_id):
        gate = self._gate(gate_id)
        with self._lock:
            gate["status"] = 0
            if GPIO:
                GPIO.output(gate["pin"], GPIO.HIGH)
        log.info("Porton %s cerrado", gate_id)
        return gate

    def pin_value(self, gate_id):
        gate = self._gate(gate_id)
        if GPIO:
            return GPIO.input(gate["pin"])
        return 1

    def status_all(self):
        return list(self.gates.values())
