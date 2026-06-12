"""Aplicacion de configuracion nueva + poller pull opcional.

La via PRIMARIA para cambiar la config es push: el admin de Aditum hace
PUT /config al Pi (ver api.py y docs/API.md). El poller pull contra el
backend queda como respaldo opcional (polling.enabled, default false).

Ambas vias convergen en apply_config(): valida contra el schema, escribe
config-runtime.json de forma atomica (tmp + rename) y decide si hay que
reiniciar el proceso (restart-on-change: evdev/VideoCapture/GPIO no se
reconfiguran en caliente; PM2 relanza con la config nueva). apply_config NO
reinicia por si misma: cada caller decide como salir (el poller inmediato,
el handler HTTP despues de responder).
"""
import json
import logging
import os
import tempfile
import threading
import time

from . import httpclient
from .settings import (
    RUNTIME_CONFIG_FILE,
    SCHEMA_FILE,
)

log = logging.getLogger("aditum.config")

try:
    import jsonschema
except ImportError:
    jsonschema = None

SUPPORTED_SCHEMA_VERSION = 1

# Campos que pueden cambiar sin reiniciar el proceso
_NO_RESTART_KEYS = {"configRevision", "polling"}

# Serializa aplicaciones concurrentes (Flask corre threaded: un PUT /config
# puede coincidir con el poller o con otro PUT)
_apply_lock = threading.Lock()


def _effective(config):
    return {k: v for k, v in config.items() if k not in _NO_RESTART_KEYS}


def _validate(config):
    """Devuelve lista de errores de validacion (vacia = config valida)."""
    if jsonschema is None:
        log.warning("jsonschema no instalado: config aplicada sin validar")
        return []
    try:
        with open(SCHEMA_FILE) as f:
            schema = json.load(f)
        validator = jsonschema.Draft7Validator(schema)
        return [e.message for e in validator.iter_errors(config)]
    except (OSError, ValueError) as e:
        log.error("No se pudo cargar el schema: %s", e)
        return [f"schema ilegible: {e}"]


def _write_atomic(config):
    fd, tmp_path = tempfile.mkstemp(
        dir=str(RUNTIME_CONFIG_FILE.parent), prefix=".config-runtime.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, RUNTIME_CONFIG_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def apply_config(new_config, settings):
    """Valida y aplica una configuracion nueva (push o pull).

    Devuelve un dict:
      {"applied": bool, "willRestart": bool, "revision": int}      exito
      {"error": str, ...detalles}                                   rechazo
    Nunca reinicia el proceso: eso lo decide el caller.
    """
    with _apply_lock:
        if new_config.get("schemaVersion") != SUPPORTED_SCHEMA_VERSION:
            return {
                "error": "unsupported schemaVersion",
                "supportedSchemaVersion": SUPPORTED_SCHEMA_VERSION,
            }

        current_revision = settings.config_revision
        new_revision = new_config.get("configRevision", 0)

        if new_revision == current_revision and new_config == settings.raw:
            # Retry idempotente: misma config, nada que hacer
            return {"applied": False, "willRestart": False, "revision": current_revision}

        if new_revision <= current_revision:
            return {"error": "stale revision", "currentRevision": current_revision}

        errors = _validate(new_config)
        if errors:
            log.error("Config revision %s invalida: %s", new_revision, "; ".join(errors))
            return {"error": "invalid config", "details": errors}

        will_restart = _effective(new_config) != _effective(settings.raw)
        _write_atomic(new_config)
        settings.raw = new_config
        settings.config_revision = new_revision
        log.info("Config aplicada: revision %s -> %s (restart=%s)",
                 current_revision, new_revision, will_restart)
        return {"applied": True, "willRestart": will_restart, "revision": new_revision}


def restart_process():
    """Salida limpia para que PM2 relance el proceso con la config nueva."""
    log.info("Reiniciando proceso para aplicar configuracion (PM2 lo relanza)")
    logging.shutdown()
    os._exit(0)


class ConfigAgent(threading.Thread):
    """Poller pull opcional (respaldo del push). Unico en el Pi."""

    def __init__(self, settings):
        super().__init__(name="config-agent", daemon=True)
        self.settings = settings
        self.interval = settings.polling_interval_seconds

    def run(self):
        if not self.settings.polling_enabled:
            log.info("Polling de configuracion deshabilitado (la config llega por push)")
            return
        if not self.settings.device_id:
            log.error("Sin device-id.txt: no se consulta config remota")
            return
        while True:
            try:
                self._poll_once()
            except Exception:
                log.exception("Error en el poller de configuracion")
            time.sleep(self.interval)

    def _poll_once(self):
        url = f"{self.settings.api_base_url}/gate-devices/{self.settings.device_id}/config"
        headers = {"If-None-Match": f'"{self.settings.config_revision}"'}
        if self.settings.device_token:
            headers["Authorization"] = f"Bearer {self.settings.device_token}"

        try:
            resp = httpclient.request("GET", url, headers=headers)
        except Exception as e:
            log.warning("Config remota inaccesible (%s); se sigue con revision %s",
                        e, self.settings.config_revision)
            return

        if resp.status_code == 304:
            return
        if resp.status_code != 200:
            log.warning("Config remota respondio %s", resp.status_code)
            return

        try:
            new_config = resp.json()
        except ValueError:
            log.error("Config remota no es JSON valido")
            return

        result = apply_config(new_config, self.settings)
        if result.get("error"):
            log.warning("Config remota rechazada: %s", result)
            return
        if result["willRestart"]:
            restart_process()
        elif result["applied"]:
            # Solo cambio polling: ajustar el intervalo sin reiniciar
            polling = new_config.get("polling", {})
            self.interval = polling.get("intervalSeconds", self.interval)
