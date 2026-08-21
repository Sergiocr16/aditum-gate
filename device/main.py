#!/usr/bin/env python3
"""Entrypoint unico del dispositivo Aditum Gate.

Un solo proceso con threads; la variante (pistola HID, camaras OpenCV,
terminales Hikvision o solo portones) la decide la configuracion, no el
codigo. PM2 lo supervisa: ante un fallo irrecuperable el proceso sale y
PM2 lo relanza.
"""
import logging

from aditum_gate.anpr import AnprService
from aditum_gate.anpr_events import AnprEventForwarder, AnprEventStore
from aditum_gate.api import create_app
from aditum_gate.backend import AditumBackend
from aditum_gate.config_agent import ConfigAgent
from aditum_gate.gpio_relays import GateController
from aditum_gate.hikvision import HikvisionService
from aditum_gate.leds import LedStrip
from aditum_gate.log import setup_logging
from aditum_gate.scanners import build_scanners
from aditum_gate.screen import ReaderHeartbeat, ScreenClient
from aditum_gate.settings import load_settings
from aditum_gate.watchdog import NetworkWatchdog

API_PORT = 8080


def main():
    setup_logging()
    log = logging.getLogger("aditum.main")

    settings = load_settings()
    log.info("Dispositivo %s (%s) | variante=%s | pantalla=%s | revision=%s",
             settings.device_id or "sin-id", settings.place_name,
             settings.scanner_type, settings.has_screen, settings.config_revision)

    ConfigAgent(settings).start()

    gates = GateController(settings)
    screen = ScreenClient(settings)
    if settings.has_screen:
        screen.reload()
        ReaderHeartbeat(settings, screen).start()
    leds = LedStrip(settings)
    backend = AditumBackend(settings)

    hikvision_service = None
    if settings.hikvision_enabled:
        hikvision_service = HikvisionService(settings)
        hikvision_service.start_nightly_cleanup()

    anpr_service = None
    anpr_store = None
    if settings.anpr_enabled:
        anpr_service = AnprService()
        anpr_store = AnprEventStore()
        AnprEventForwarder(settings, anpr_store).start()

    for scanner in build_scanners(settings, backend, screen, leds):
        scanner.start()

    if settings.watchdog_enabled:
        NetworkWatchdog(settings).start()

    app = create_app(settings, gates, hikvision_service, screen, leds,
                 anpr_service=anpr_service, anpr_store=anpr_store)
    # threaded=True explicito: es el default de Flask >=1.0, pero el API
    # DEPENDE de atender requests concurrentes (un pulso de porton de 1 s
    # o un ISAPI a Hikvision no pueden bloquear el health check) — se
    # pinnea para que un cambio de default aguas arriba no lo degrade.
    app.run(host="0.0.0.0", port=API_PORT, threaded=True)


if __name__ == "__main__":
    main()
