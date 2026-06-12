"""Tira NeoPixel opcional (pedestales con LED). Habilitada por config
(gpio.neopixel.enabled); si la libreria no esta instalada queda en no-op."""
import logging
import threading
import time

log = logging.getLogger("aditum.leds")

WHITE = (255, 255, 255)
GREEN = (0, 255, 0)
RED = (255, 0, 0)
OFF = (0, 0, 0)


class LedStrip:
    def __init__(self, settings):
        self.pixels = None
        if not settings.neopixel_enabled:
            return
        try:
            import board
            import neopixel
            pin = getattr(board, settings.neopixel_pin)
            self.pixels = neopixel.NeoPixel(pin, settings.neopixel_count,
                                            brightness=1, auto_write=False)
            self.set_color(WHITE)
        except Exception as e:
            log.error("NeoPixel habilitado pero no inicializable: %s", e)
            self.pixels = None

    def set_color(self, color):
        if self.pixels is None:
            return
        self.pixels.fill(color)
        self.pixels.show()

    def _flash(self, color, seconds):
        self.set_color(color)
        time.sleep(seconds)
        self.set_color(WHITE)

    def flash_green(self, seconds=3):
        """Asincrono: no bloquea el loop del scanner."""
        threading.Thread(target=self._flash, args=(GREEN, seconds), daemon=True).start()

    def flash_red(self, seconds=3):
        threading.Thread(target=self._flash, args=(RED, seconds), daemon=True).start()

    def turn_off(self):
        self.set_color(OFF)
