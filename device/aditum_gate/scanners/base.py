"""Logica comun a todos los scanners (HID y OpenCV).

Cada lector configurado corre como un thread independiente. La validacion de
prefijo, el debounce de codigos repetidos y la verificacion contra el backend
viven aqui UNA sola vez; las subclases solo implementan read_code().

Prefijos: se aceptan SIEMPRE los dos formatos de QR de Aditum ("ADTG..." y
"ADITUMGATE=...") y solo esos; el prefijo leido decide el endpoint del
backend (ver backend.QR_FORMATS). No depende de la config del equipo.
"""
import logging
import threading
import time

from ..backend import QR_PREFIXES, match_qr

log = logging.getLogger("aditum.scanner")

# Ignorar relecturas del mismo codigo durante este lapso (reemplaza los
# time.sleep(3) bloqueantes de los scripts originales)
DEBOUNCE_SECONDS = 3
MAX_CODE_LEN = 256


class Scanner(threading.Thread):
    def __init__(self, reader, settings, backend, screen, leds):
        super().__init__(name=f"scanner-{reader.role}", daemon=True)
        self.reader = reader
        self.settings = settings
        self.backend = backend
        self.screen = screen
        self.leds = leds
        # Prefijos validos (para la guardia temprana del lector HID)
        self.prefixes = QR_PREFIXES
        self._last_code = None
        self._last_code_at = 0.0

    # ------------------------------------------------------------
    # A implementar por cada tipo de scanner
    # ------------------------------------------------------------
    def read_code(self):
        """Bloquea hasta leer un codigo crudo. None para reintentar."""
        raise NotImplementedError

    # ------------------------------------------------------------
    def run(self):
        log.info("Scanner %s (%s) iniciado", self.reader.role, type(self).__name__)
        while True:
            try:
                code = self.read_code()
            except Exception:
                log.exception("Error leyendo del scanner %s; reintento en 2s", self.reader.role)
                time.sleep(2)
                continue
            if code:
                self.process(code)

    def process(self, raw):
        text = raw.strip()
        if not text:
            return

        if self._is_duplicate(text):
            log.debug("Codigo repetido ignorado (%s)", self.reader.role)
            return

        matched = match_qr(text)
        if matched is None or len(text) > MAX_CODE_LEN:
            log.info("QR invalido en %s: prefijo/longitud incorrectos", self.reader.role)
            self.deny()
            return

        prefix, style = matched
        payload = text[len(prefix):]
        if not payload:
            log.info("QR sin datos tras el prefijo en %s: denegado local", self.reader.role)
            self.deny()
            return

        # Semantica estricta opcional (scannerExit.py original): si el
        # marcador EXIT del QR no coincide con el rol del lector se deniega
        # localmente, sin consultar al backend.
        if self.reader.strict_marker_match and \
                (self.reader.role == "exit") != ("EXIT" in payload):
            log.info("Marcador EXIT no coincide con el rol %s: denegado local",
                     self.reader.role)
            self.deny()
            return

        self.screen.loading()
        authorized = self.backend.verify(self.reader.role, payload, self.reader.door_id, style)
        if authorized:
            if self.leds:
                self.leds.flash_green()
        else:
            self.deny()

    def deny(self):
        self.screen.denied(door_type=self.reader.role, door_id=self.reader.door_id)
        if self.leds:
            self.leds.flash_red()

    def _is_duplicate(self, text):
        now = time.monotonic()
        if text == self._last_code and (now - self._last_code_at) < DEBOUNCE_SECONDS:
            self._last_code_at = now
            return True
        self._last_code = text
        self._last_code_at = now
        return False
