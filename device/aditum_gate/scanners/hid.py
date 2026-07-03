"""Lector QR tipo pistola (teclado HID via evdev).

Reemplaza scannerQr.py y scannerQrExit.py: entry y exit son dos instancias
de esta misma clase, parametrizadas por la config. Soporta dos pistolas del
mismo modelo con asignacion determinista: los lectores se ordenan por su
puerto fisico USB (Phys=) y cada instancia toma el que le toca por posicion
en la config (o el fijado por devicePhys); una vez conectado queda anclado
a ese puerto para reconectarse al mismo aparato. Reconexion en caliente si
el lector se desenchufa.
"""
import logging
import threading
import time

import evdev
from evdev import categorize, ecodes

from .base import Scanner

log = logging.getLogger("aditum.scanner.hid")

SCANCODES = {
    0: None, 1: 'ESC', 2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8',
    10: '9', 11: '0', 12: '-', 13: '=', 14: 'BKSP', 15: 'TAB', 16: 'q', 17: 'w', 18: 'e', 19: 'r',
    20: 't', 21: 'y', 22: 'u', 23: 'i', 24: 'o', 25: 'p', 26: '[', 27: ']', 28: 'CR', 29: 'LCTRL',
    30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h', 36: 'j', 37: 'k', 38: 'l', 39: ';',
    40: '"', 41: '`', 42: 'LSHFT', 43: '\\', 44: 'z', 45: 'x', 46: 'c', 47: 'v', 48: 'b', 49: 'n',
    50: 'm', 51: ',', 52: '.', 53: '/', 54: 'RSHFT', 56: 'LALT', 57: ' ', 100: 'RALT',
    # Keypad numerico (algunos lectores viejos lo usan)
    79: '1', 80: '2', 81: '3', 75: '4', 76: '5',
    77: '6', 71: '7', 72: '8', 73: '9', 82: '0',
}

CAPS_CODES = {
    0: None, 1: 'ESC', 2: '!', 3: '@', 4: '#', 5: '$', 6: '%', 7: '^', 8: '&', 9: '*',
    10: '(', 11: ')', 12: '_', 13: '+', 14: 'BKSP', 15: 'TAB', 16: 'Q', 17: 'W', 18: 'E', 19: 'R',
    20: 'T', 21: 'Y', 22: 'U', 23: 'I', 24: 'O', 25: 'P', 26: '{', 27: '}', 28: 'CR', 29: 'LCTRL',
    30: 'A', 31: 'S', 32: 'D', 33: 'F', 34: 'G', 35: 'H', 36: 'J', 37: 'K', 38: 'L', 39: ':',
    40: "'", 41: '~', 42: 'LSHFT', 43: '|', 44: 'Z', 45: 'X', 46: 'C', 47: 'V', 48: 'B', 49: 'N',
    50: 'M', 51: '<', 52: '>', 53: '?', 54: 'RSHFT', 56: 'LALT', 57: ' ', 100: 'RALT',
}

NON_CHAR_KEYS = {'LSHFT', 'RSHFT', 'CR', 'BKSP', 'TAB', 'ESC', 'LALT', 'RALT', 'LCTRL', 'RCTRL', ' '}
SHIFT_SCANCODES = {42, 54}

# Puertos fisicos (Phys=) ya reclamados por otra instancia (dos pistolas
# del mismo modelo no deben tomar el mismo aparato)
_claimed_phys = set()
_claimed_lock = threading.Lock()

# Iteraciones de espera (2s c/u) antes de soltar el anclaje al puerto
# fisico y aceptar el lector en otro puerto (lo movieron de enchufe)
PIN_MISS_LIMIT = 30


def _find_devices_by_name(device_name, device_phys=None):
    """Lectores cuyo nombre contenga device_name: [(phys, path)] ordenado.

    Un aparato fisico puede exponer varios nodos de input (teclado +
    control): se agrupa por el puerto fisico (Phys= sin el sufijo /inputN)
    y se toma el eventX mas bajo, que es el del teclado. El orden por phys
    es estable entre reinicios (depende del enchufe, no de la enumeracion).
    """
    wanted = (device_name or "").lower()
    phys_filter = (device_phys or "").lower()
    by_phys = {}
    try:
        with open("/proc/bus/input/devices") as f:
            lines = f.readlines()
    except OSError as e:
        log.error("No se pudo leer /proc/bus/input/devices: %s", e)
        return []

    current_name = None
    current_phys = None
    current_handlers = None
    for line in lines + [""]:
        line = line.strip()
        if line.startswith("N: Name="):
            current_name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("P: Phys="):
            current_phys = line.split("=", 1)[1].strip()
        elif line.startswith("H: Handlers="):
            current_handlers = line.split("=", 1)[1].strip()
        elif line == "":
            if current_name and current_handlers and wanted and wanted in current_name.lower():
                for token in current_handlers.split():
                    if not token.startswith("event"):
                        continue
                    try:
                        event_n = int(token[len("event"):])
                    except ValueError:
                        continue
                    path = f"/dev/input/{token}"
                    phys = (current_phys or path).split("/input")[0]
                    if phys_filter and phys_filter not in phys.lower():
                        continue
                    known = by_phys.get(phys)
                    if known is None or event_n < known[0]:
                        by_phys[phys] = (event_n, path)
            current_name = None
            current_phys = None
            current_handlers = None
    return sorted((phys, item[1]) for phys, item in by_phys.items())


class HidScanner(Scanner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._device = None
        self._device_phys = None
        # Rango entre lectores identicos (mismo deviceName/devicePhys): el
        # primero de la config toma el puerto fisico mas bajo. Determinista
        # entre reinicios, a diferencia de "el primer thread que llegue".
        twins = [r for r in self.settings.scanners
                 if r.device_name == self.reader.device_name
                 and r.device_phys == self.reader.device_phys]
        self._rank = twins.index(self.reader)
        self._pinned_phys = None  # puerto fisico al que quedo anclado
        self._ever_pinned = False
        self._pin_misses = 0

    # ------------------------------------------------------------
    # Conexion / reconexion
    # ------------------------------------------------------------
    def _pick_candidate(self, candidates):
        """Con que (phys, path) quedarse en esta pasada, o None."""
        if self._pinned_phys:
            for phys, path in candidates:
                if phys == self._pinned_phys:
                    self._pin_misses = 0
                    return phys, path
            # El lector no reaparece en su puerto: tras un rato, aceptar
            # que lo movieron de enchufe y tomar cualquier lector libre.
            self._pin_misses += 1
            if self._pin_misses >= PIN_MISS_LIMIT:
                log.warning("Lector %s ausente de su puerto %s; se acepta otro puerto",
                            self.reader.role, self._pinned_phys)
                self._pinned_phys = None
                self._pin_misses = 0
            return None
        if not self._ever_pinned:
            # Primera asignacion: por rango sobre la lista completa, para
            # que entry/exit no dependan de que thread gano la carrera
            if len(candidates) > self._rank:
                candidate = candidates[self._rank]
                if candidate[0] not in _claimed_phys:
                    return candidate
            return None
        # Post-anclaje perdido: cualquier lector libre (disponibilidad
        # antes que determinismo, ya lo movieron de puerto)
        free = [c for c in candidates if c[0] not in _claimed_phys]
        return free[0] if free else None

    def _acquire_device(self):
        """Bloquea hasta reclamar un lector que coincida por nombre/puerto."""
        announced = False
        while True:
            with _claimed_lock:
                candidates = _find_devices_by_name(self.reader.device_name,
                                                   self.reader.device_phys)
                picked = self._pick_candidate(candidates)
                if picked:
                    phys, path = picked
                    try:
                        device = evdev.InputDevice(path)
                        device.grab()  # exclusivo: evita inyeccion en la terminal
                        _claimed_phys.add(phys)
                        self._device = device
                        self._device_phys = phys
                        self._pinned_phys = phys
                        self._ever_pinned = True
                        self._pin_misses = 0
                        log.info("Lector %s conectado en %s (%s)",
                                 self.reader.role, path, phys)
                        return
                    except OSError as e:
                        log.warning("No se pudo abrir %s: %s", path, e)
            if not announced:
                log.warning("Lector '%s' (%s) no encontrado; esperando conexion...",
                            self.reader.device_name, self.reader.role)
                announced = True
            time.sleep(2)

    def _release_device(self):
        with _claimed_lock:
            _claimed_phys.discard(self._device_phys)
        if self._device is not None:
            try:
                self._device.close()
            except OSError:
                pass
        self._device = None
        self._device_phys = None

    # ------------------------------------------------------------
    def read_code(self):
        if self._device is None:
            self._acquire_device()
        try:
            return self._read_until_terminator()
        except OSError as e:
            log.warning("Lector %s desconectado (%s); intentando reconectar",
                        self.reader.role, e)
            self._release_device()
            return None

    def _read_until_terminator(self):
        """Lee teclas hasta Enter (lectores nuevos) o '@' (sufijo legacy).

        Bloquea la lectura apenas el texto deja de coincidir con el prefijo
        esperado (modo discarding hasta el siguiente terminador).
        """
        caps = False
        current_text = ""
        discarding = False

        for event in self._device.read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            data = categorize(event)

            if data.scancode in SHIFT_SCANCODES:
                caps = (data.keystate == 1)
                continue
            if data.keystate != 1:  # solo key down
                continue

            key = CAPS_CODES.get(data.scancode) if caps else SCANCODES.get(data.scancode, '')
            if key is None:
                continue

            is_terminator = key == 'CR' or key == '@'
            if is_terminator:
                if discarding:
                    discarding = False
                    current_text = ""
                    continue
                if current_text:
                    log.debug("Codigo leido en %s", self.reader.role)
                    return current_text
                continue

            if key in NON_CHAR_KEYS or discarding:
                continue

            current_text += key

            # Guardia de prefijo inmediata (case-insensitive)
            typed = len(current_text)
            prefix = self.prefix.upper()
            if typed <= len(prefix) and prefix[:typed] != current_text.upper():
                log.info("Prefijo invalido en %s: lectura bloqueada", self.reader.role)
                self.deny()
                discarding = True
                current_text = ""
                continue

            if typed > 256:
                log.info("Codigo demasiado largo en %s: lectura bloqueada", self.reader.role)
                self.deny()
                discarding = True
                current_text = ""

    def process(self, raw):
        # El terminador '@' legacy ya no viene incluido (se consume al leer),
        # pero por si el lector envia '@' + CR, limpiarlo.
        super().process(raw.rstrip('@'))
