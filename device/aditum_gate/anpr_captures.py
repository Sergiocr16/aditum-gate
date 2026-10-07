"""Bitacora local de placas NO reconocidas, con la foto que manda la camara.

Cuando una camara ANPR no reconoce una placa (OCR ilegible, "unknown") o la
lee pero no esta en su allow list, la unica forma de saber QUE vio la camara
es la foto del evento. La camara la manda en el mismo multipart del
EventNotificationAlert (licensePlatePicture.jpg = recorte de la placa,
detectionPicture.jpg = escena completa), pero hasta ahora se tiraba: la cola
de eventos (anpr_events) solo guarda placa y fecha, y las ilegibles ni eso.

Este modulo guarda, en el disco de la Pi, un registro por lectura no
reconocida: un JSON con fecha/hora del evento, la placa (si la hubo), la
lista que reporto la camara y el motivo, mas los JPG tal cual llegaron. Es
diagnostico puro: nada de esto se reenvia a Aditum ni abre portones.

Decisiones:
  - Archivos planos, no SQLite: hay que poder bajar un JPG con `scp` o verlo
    desde /admin sin herramientas. El nombre del JSON ya es la bitacora:
    `<fecha-hora>_<placa|SIN-PLACA>_<uid>.json`, y las fotos comparten el
    prefijo (`..._<n>-<parte>.jpg`). Listar es leer el directorio en orden
    inverso.
  - Purga automatica por EDAD (anpr.captureRetentionDays, default 15) y por
    TAMANO (anpr.captureMaxMb): una camara que mira a la calle puede sumar
    cientos de fotos por dia, y la SD de una Pi no es infinita. La corre el
    forwarder ANPR una vez por hora, junto con la purga de la cola.
  - El nombre de archivo es la unica entrada al disco desde el API: se
    valida contra una regex estricta (SAFE_NAME) antes de servir una foto.
  - Guarda de espacio libre (MIN_FREE_MB): con la SD casi llena las fotos
    dejan de guardarse (el JSON si, pesa nada) aunque el tope en MB no se
    haya alcanzado. Un disco lleno tumba la cola SQLite, los logs y la
    escritura de config: ninguna foto de diagnostico vale eso.
"""
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .settings import ANPR_CAPTURES_DIR

log = logging.getLogger("aditum.anpr.captures")

REASON_UNREADABLE = "unreadable"       # la camara no pudo leer la placa
REASON_NOT_AUTHORIZED = "not_authorized"  # leida, pero no esta en el allow list

NO_PLATE_LABEL = "SIN-PLACA"
MIN_FREE_MB = 1024          # por debajo de esto no se guardan fotos
LOW_DISK_LOG_INTERVAL = 600  # segundos entre avisos de disco lleno
STAMP_FORMAT = "%Y%m%d-%H%M%S"
MAX_IMAGES_PER_EVENT = 4
# Nombres que se aceptan para leer del disco: solo lo que genera este modulo.
SAFE_NAME = re.compile(
    r"^(?P<date>[0-9]{8})-(?P<time>[0-9]{6})_(?P<plate>[A-Z0-9-]{1,16})_(?P<uid>[0-9a-f]{8})"
    r"(_[0-9]-[a-z0-9-]{1,40})?\.(json|jpg)$")
_UNSAFE_CHARS = re.compile(r"[^A-Z0-9-]")
_UNSAFE_PART = re.compile(r"[^a-z0-9-]")
_NON_HEX = re.compile(r"[^0-9a-f]")


def _safe_plate(plate):
    """La placa como la escribe la camara, reducida a [A-Z0-9-] para el
    nombre de archivo. El JSON guarda la original sin tocar."""
    if not plate:
        return NO_PLATE_LABEL
    cleaned = _UNSAFE_CHARS.sub("", plate.upper())[:16]
    return cleaned or NO_PLATE_LABEL


def _safe_part(name):
    """Nombre de la parte del multipart (licensePlatePicture.jpg...) como
    sufijo corto del archivo de imagen."""
    base = (name or "").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    cleaned = _UNSAFE_PART.sub("", base)[:40]
    return cleaned or "imagen"


class AnprCaptureStore:
    def __init__(self, path=ANPR_CAPTURES_DIR, retention_days=15, max_mb=500):
        self.dir = Path(path)
        self.retention_days = retention_days
        self.max_bytes = max_mb * 1024 * 1024
        self._lock = threading.Lock()
        self._last_low_disk_log = 0.0
        self.dir.mkdir(parents=True, exist_ok=True)

    def disk_free_mb(self):
        try:
            return shutil.disk_usage(self.dir).free // (1024 * 1024)
        except OSError:
            return None

    def _has_room_for_images(self):
        free = self.disk_free_mb()
        if free is None or free >= MIN_FREE_MB:
            return True
        now = time.monotonic()
        if now - self._last_low_disk_log >= LOW_DISK_LOG_INTERVAL:
            self._last_low_disk_log = now
            log.warning("Disco casi lleno (%s MB libres < %s): la bitacora de placas "
                        "no reconocidas guarda el registro pero NO las fotos",
                        free, MIN_FREE_MB)
        return False

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------
    def save(self, event, images, source_ip=None, reason=REASON_NOT_AUTHORIZED):
        """Guarda la lectura y sus fotos. `event` es el dict de
        parse_event_xml (licensePlate None cuando fue ilegible); `images`
        es una lista de (nombre, bytes). Devuelve el registro guardado, o
        None si el evento ya estaba (reintento de la camara)."""
        # Solo hex del eventUid: el nombre tiene que pasar SAFE_NAME o el
        # registro no se listaria ni se purgaria nunca.
        uid = _NON_HEX.sub("", (event.get("eventUid") or "").lower())[:8]
        uid = uid.ljust(8, "0") if uid else "00000000"
        received_at = datetime.now().astimezone()
        base = "%s_%s_%s" % (received_at.strftime(STAMP_FORMAT),
                             _safe_plate(event.get("licensePlate")), uid)
        record = {
            "name": base + ".json",
            "receivedAt": received_at.isoformat(timespec="seconds"),
            "capturedAt": event.get("capturedAt"),
            "licensePlate": event.get("licensePlate"),
            "vehicleList": event.get("vehicleList", ""),
            "confidenceLevel": event.get("confidenceLevel"),
            "cameraName": event.get("cameraName", ""),
            "sourceIp": source_ip or "",
            "reason": reason,
            "eventUid": event.get("eventUid"),
            "picturesDeclared": event.get("pictureCount"),
            "images": [],
        }
        with self._lock:
            if uid != "00000000" and any(self.dir.glob("*_%s.json" % uid)):
                return None
            if images and not self._has_room_for_images():
                images = []
                record["imagesSkipped"] = "disco lleno"
            for index, (name, data) in enumerate(images[:MAX_IMAGES_PER_EVENT]):
                if not data:
                    continue
                image_name = "%s_%d-%s.jpg" % (base, index, _safe_part(name))
                try:
                    (self.dir / image_name).write_bytes(data)
                except OSError as e:
                    log.error("No se pudo guardar la foto %s: %s", image_name, e)
                    continue
                record["images"].append(image_name)
            tmp = self.dir / (record["name"] + ".tmp")
            try:
                tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1))
                os.replace(tmp, self.dir / record["name"])
            except OSError as e:
                log.error("No se pudo guardar la bitacora %s: %s", record["name"], e)
                return None
        log.info("Placa no reconocida guardada: %s (%s, %s foto(s), lista=%r)",
                 record["name"], reason, len(record["images"]),
                 record["vehicleList"])
        return record

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    def _records(self):
        """Los JSON del directorio, del mas nuevo al mas viejo (el nombre
        empieza por la fecha, asi que el orden alfabetico inverso es el
        cronologico inverso)."""
        return sorted((p for p in self.dir.glob("*.json") if SAFE_NAME.match(p.name)),
                      key=lambda p: p.name, reverse=True)

    @staticmethod
    def _matches(match, date=None, time_from=None, time_to=None, plate=None,
                 reason=None):
        """Filtros sobre las partes del NOMBRE (fecha-hora de recepcion en la
        Pi, placa saneada, SIN-PLACA para ilegibles): no hace falta abrir
        ningun JSON para filtrar miles de registros.

        date: 'YYYYMMDD'. time_from/time_to: 'HHMMSS' inclusive. plate:
        subcadena ya saneada ([A-Z0-9-]). reason: REASON_UNREADABLE |
        REASON_NOT_AUTHORIZED."""
        if date and match["date"] != date:
            return False
        if time_from and match["time"] < time_from:
            return False
        if time_to and match["time"] > time_to:
            return False
        if plate and plate not in match["plate"]:
            return False
        if reason == REASON_UNREADABLE and match["plate"] != NO_PLATE_LABEL:
            return False
        if reason == REASON_NOT_AUTHORIZED and match["plate"] == NO_PLATE_LABEL:
            return False
        return True

    def list(self, limit=50, offset=0, **filters):
        """Registros del mas nuevo al mas viejo que cumplen los filtros (ver
        _matches). Devuelve (registros de la pagina, total que cumple)."""
        matched = [p for p in self._records()
                   if self._matches(SAFE_NAME.match(p.name), **filters)]
        out = []
        for path in matched[offset:offset + limit]:
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, ValueError) as e:
                log.warning("Registro de captura ilegible %s: %s", path.name, e)
        return out, len(matched)

    def days(self):
        """Cuantos registros hay por dia (YYYY-MM-DD), del mas reciente al
        mas viejo. Para que el visor sepa que fechas tienen algo."""
        counts = {}
        for path in self._records():
            raw = SAFE_NAME.match(path.name)["date"]
            key = "%s-%s-%s" % (raw[:4], raw[4:6], raw[6:])
            counts[key] = counts.get(key, 0) + 1
        return counts

    def file_path(self, name):
        """Ruta de una foto o JSON de la bitacora, o None si el nombre no es
        de los que genera este modulo (unica defensa contra ../)."""
        if not SAFE_NAME.match(name or ""):
            return None
        path = self.dir / name
        return path if path.is_file() else None

    def stats(self):
        count = 0
        total = 0
        for path in self.dir.iterdir():
            if not SAFE_NAME.match(path.name):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if path.suffix == ".json":
                count += 1
        return {"count": count, "bytes": total,
                "retentionDays": self.retention_days,
                "maxMb": self.max_bytes // (1024 * 1024),
                "diskFreeMb": self.disk_free_mb(),
                "minFreeMb": MIN_FREE_MB}

    # ------------------------------------------------------------------
    # Purga
    # ------------------------------------------------------------------
    def _delete_record(self, json_path):
        """Borra el JSON y todas sus fotos (mismo prefijo)."""
        prefix = json_path.name[:-len(".json")]
        removed = 0
        for path in self.dir.glob(prefix + "*"):
            try:
                path.unlink()
                removed += 1
            except OSError as e:
                log.warning("No se pudo borrar %s: %s", path.name, e)
        return removed

    def purge(self):
        """Borra lo mas viejo que retention_days y, si aun asi el directorio
        pesa mas que max_bytes, sigue borrando del mas viejo al mas nuevo
        hasta entrar. Devuelve cuantos registros borro."""
        cutoff = (datetime.now() - timedelta(days=self.retention_days)) \
            .strftime(STAMP_FORMAT)
        purged = 0
        with self._lock:
            records = self._records()  # mas nuevo primero
            keep = []
            for path in records:
                if path.name[:len(cutoff)] < cutoff:
                    self._delete_record(path)
                    purged += 1
                else:
                    keep.append(path)
            total = self.stats()["bytes"]
            while keep and total > self.max_bytes:
                oldest = keep.pop()
                prefix = oldest.name[:-len(".json")]
                for path in self.dir.glob(prefix + "*"):
                    try:
                        total -= path.stat().st_size
                    except OSError:
                        pass
                self._delete_record(oldest)
                purged += 1
        if purged:
            log.info("Purga de placas no reconocidas: %s registro(s) borrados "
                     "(retencion %s dias, tope %s MB)", purged,
                     self.retention_days, self.max_bytes // (1024 * 1024))
        return purged

    def clear(self):
        """Borra toda la bitacora (boton del editor). Devuelve cuantos
        registros borro."""
        with self._lock:
            records = self._records()
            for path in records:
                self._delete_record(path)
            return len(records)
