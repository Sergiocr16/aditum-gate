"""Carga y acceso tipado a la configuracion del dispositivo.

La config se lee UNA vez al arrancar el proceso. Cuando el config_agent
detecta una revision nueva, reinicia el proceso (PM2 lo vuelve a levantar)
y este modulo carga la version nueva.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("aditum.settings")

REPO_ROOT = Path(__file__).resolve().parents[2]

RUNTIME_CONFIG_FILE = REPO_ROOT / "config-runtime.json"
DEFAULT_CONFIG_FILE = REPO_ROOT / "config-default.json"
SCHEMA_FILE = REPO_ROOT / "config.schema.json"
DEVICE_ID_FILE = REPO_ROOT / "device-id.txt"
DEVICE_TOKEN_FILE = REPO_ROOT / "device-token.txt"
CARD_STORE_FILE = REPO_ROOT / "hikvision-cards.json"

SCREEN_BASE_URL = "http://localhost:3000"

# Prefijo del QR segun el estilo de verificacion del backend
QR_PREFIXES = {"secure": "ADTG", "legacy": "ADITUMGATE="}


def _read_text_file(path):
    try:
        if path.exists():
            value = path.read_text().strip()
            return value or None
    except OSError as e:
        log.error("No se pudo leer %s: %s", path, e)
    return None


class Reader:
    """Un lector configurado (pistola HID o camara OpenCV)."""

    def __init__(self, data):
        self.role = data["role"]  # "entry" | "exit"
        self.door_id = data["doorId"]
        self.device_name = data.get("deviceName")
        self.camera_index = data.get("cameraIndex")
        self.show_camera_feed = data.get("showCameraFeed", False)


class Settings:
    def __init__(self, data, source):
        self.raw = data
        self.source = source  # path del archivo del que se cargo

        self.schema_version = data.get("schemaVersion", 1)
        self.config_revision = data.get("configRevision", 0)
        self.device_id = _read_text_file(DEVICE_ID_FILE) or data.get("deviceId") or ""
        self.device_token = _read_text_file(DEVICE_TOKEN_FILE)
        self.place_name = data.get("placeName", "")

        api = data.get("api", {})
        self.api_base_url = api.get("baseUrl", "https://app.aditumcr.com/api").rstrip("/")
        self.verifier_style = api.get("verifierStyle", "secure")
        self.qr_prefix = QR_PREFIXES[self.verifier_style]

        self.scanner_type = data.get("scannerType", "none")
        self.scanners = [Reader(r) for r in data.get("scanners", [])]

        screen = data.get("screen", {})
        self.has_screen = screen.get("hasScreen", False)
        self.screen_door_type = screen.get("doorType", "ENTRY")
        self.client_logo_url = screen.get("clientLogoUrl", "")

        gpio = data.get("gpio", {})
        self.gpio_enabled = gpio.get("enabled", True)
        self.gpio_mode = gpio.get("mode", "BOARD")
        self.gates = gpio.get("gates", [])
        self.pulse_seconds = gpio.get("pulseSeconds", 1)

        neopixel = gpio.get("neopixel", {})
        self.neopixel_enabled = neopixel.get("enabled", False)
        self.neopixel_pin = neopixel.get("pin", "D18")
        self.neopixel_count = neopixel.get("count", 55)

        watchdog = gpio.get("watchdog", {})
        self.watchdog_enabled = watchdog.get("enabled", False)
        self.watchdog_host = watchdog.get("host", "1.1.1.1")
        self.watchdog_interval_sec = watchdog.get("intervalSec", 240)

        hik = data.get("hikvision", {})
        self.hikvision_enabled = hik.get("enabled", False) or self.scanner_type == "hikvision"
        self.nightly_cleanup_hour = hik.get("nightlyCleanupHour", 2)

        # El push (PUT /config desde Aditum) es la via primaria; el poller
        # pull es un respaldo que se habilita explicitamente.
        polling = data.get("polling", {})
        self.polling_enabled = polling.get("enabled", False)
        self.polling_interval_seconds = polling.get("intervalSeconds", 60)


def load_settings():
    """Config runtime (cache de la remota) si existe; si no, la default."""
    for path in (RUNTIME_CONFIG_FILE, DEFAULT_CONFIG_FILE):
        try:
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
                log.info("Configuracion cargada de %s (revision %s)",
                         path.name, data.get("configRevision"))
                return Settings(data, path)
        except (OSError, ValueError) as e:
            log.error("Config %s invalida, se ignora: %s", path, e)
    raise RuntimeError("No hay configuracion utilizable (falta config-default.json)")
