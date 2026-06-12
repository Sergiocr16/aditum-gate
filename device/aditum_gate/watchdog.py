"""Watchdog de red: si el Pi pierde conectividad, reinicia el sistema.

Portado del index.js de la variante qr-small. Habilitado por config
(gpio.watchdog.enabled). Reemplaza al cron que reiniciaba todo cada 10
minutos hubiera o no problema.
"""
import logging
import subprocess
import threading
import time

log = logging.getLogger("aditum.watchdog")

FIRST_CHECK_DELAY = 60  # dar tiempo a que la red levante tras el boot


class NetworkWatchdog(threading.Thread):
    def __init__(self, settings):
        super().__init__(name="watchdog", daemon=True)
        self.host = settings.watchdog_host
        self.interval = settings.watchdog_interval_sec

    def run(self):
        log.info("Watchdog de red activo: ping a %s cada %ss", self.host, self.interval)
        time.sleep(FIRST_CHECK_DELAY)
        while True:
            if not self._ping():
                log.error("Sin red contra %s: reiniciando el sistema", self.host)
                subprocess.call("sudo /sbin/reboot", shell=True)
            time.sleep(self.interval)

    def _ping(self):
        result = subprocess.call(
            ["ping", "-c", "1", "-W", "2", self.host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result == 0
