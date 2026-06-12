"""Logica comun a todos los scanners (HID y OpenCV).

Cada lector configurado corre como un thread independiente. La validacion de
prefijo, el debounce de codigos repetidos y la verificacion contra el backend
viven aqui UNA sola vez; las subclases solo implementan read_code().
"""
import logging
import threading
import time

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
        self.prefix = settings.qr_prefix
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

        if not text.upper().startswith(self.prefix.upper()) or len(text) > MAX_CODE_LEN:
            log.info("QR invalido en %s: prefijo/longitud incorrectos", self.reader.role)
            self.deny()
            return

        payload = text[len(self.prefix):]
        self.screen.loading()
        authorized = self.backend.verify(self.reader.role, payload, self.reader.door_id)
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
