"""Construccion de scanners segun la configuracion del dispositivo."""
import logging

log = logging.getLogger("aditum.scanners")


def build_scanners(settings, backend, screen, leds):
    """Devuelve la lista de threads de scanner a arrancar (0, 1 o 2)."""
    if settings.scanner_type == "hid":
        from .hid import HidScanner
        cls = HidScanner
    elif settings.scanner_type == "opencv":
        from .opencv import OpenCvScanner
        cls = OpenCvScanner
    else:
        # "hikvision" y "none" no tienen scanner local
        return []

    scanners = []
    for reader in settings.scanners:
        scanners.append(cls(reader, settings, backend, screen, leds))
        log.info("Scanner %s configurado: %s puerta %s",
                 settings.scanner_type, reader.role, reader.door_id)
    return scanners
