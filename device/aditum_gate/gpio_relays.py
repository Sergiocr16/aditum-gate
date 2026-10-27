"""Control de relays (portones) por GPIO.

Los pines vienen de la config (gpio.gates); el default en config-default.json
reproduce los 10 relays historicos en modo BOARD.

Portones relacionados (gpio.gates[].linkedGates): al abrir un porton, cada
porton relacionado se pulsa delaySeconds despues, en un Timer propio, sin
bloquear la respuesta HTTP. No encadena: los relacionados del relacionado no
se siguen (evita ciclos 1 -> 2 -> 1). Si se quiere 1 -> 2 -> 3, se declaran
ambos en el porton 1 con delays crecientes.
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
                                "linkedGates": list(g.get("linkedGates") or []),
                                "status": 0}
                      for g in settings.gates}
        self._lock = threading.Lock()
        self._timers = []  # Timers de portones relacionados pendientes

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
        self._prune_links()

    def _prune_links(self):
        """Descarta links a portones inexistentes (o a si mismo) con ERROR en
        vez de fallar al abrir: el porton principal sigue funcionando."""
        for gate in self.gates.values():
            valid = []
            for link in gate["linkedGates"]:
                target = link.get("gateId")
                if target == gate["id"] or target not in self.gates:
                    log.error("Porton %s: porton relacionado %s no existe (o es el "
                              "mismo) — se ignora", gate["id"], target)
                    continue
                valid.append({"gateId": target,
                              "delaySeconds": float(link.get("delaySeconds", 0))})
            gate["linkedGates"] = valid

    def _gate(self, gate_id):
        gate = self.gates.get(gate_id)
        if gate is None:
            raise KeyError(f"Porton {gate_id} no existe en la configuracion")
        return gate

    def open_gate(self, gate_id, follow_links=True):
        """Pulso de apertura: nivel activo durante pulse_seconds y de vuelta a reposo.

        Con follow_links, agenda ademas los portones relacionados. El delay se
        cuenta desde este disparo, pero los Timers arrancan recien despues del
        pulso (descontando lo ya transcurrido): asi el principal siempre va
        primero, aun con delay 0."""
        gate = self._gate(gate_id)
        started = time.monotonic()
        self._pulse(gate)
        log.info("Porton %s abierto (pulso %ss)", gate_id, self.pulse_seconds)
        if follow_links:
            elapsed = time.monotonic() - started
            for link in gate["linkedGates"]:
                self._schedule_linked(gate_id, link, max(0.0, link["delaySeconds"] - elapsed))
        return gate

    def _pulse(self, gate):
        with self._lock:
            gate["status"] = 1
            if GPIO:
                idle, active = _levels(gate)
                GPIO.output(gate["pin"], active)
                time.sleep(self.pulse_seconds)
                GPIO.output(gate["pin"], idle)
            gate["status"] = 0

    def _schedule_linked(self, source_id, link, delay):
        target_id = link["gateId"]

        def fire():
            try:
                # follow_links=False: el relacionado no arrastra a los suyos
                self.open_gate(target_id, follow_links=False)
                log.info("Porton %s abierto por relacion con %s (%.1fs despues)",
                         target_id, source_id, link["delaySeconds"])
            except Exception:
                log.exception("Porton relacionado %s (desde %s) fallo", target_id, source_id)
            finally:
                self._timers = [t for t in self._timers if t is not timer]

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()
        log.info("Porton %s: porton relacionado %s agendado en %.1fs (delay %.1fs)",
                 source_id, target_id, delay, link["delaySeconds"])

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
