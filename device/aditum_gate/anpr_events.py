"""Receptor local de eventos ANPR + cola offline + reenvio a Aditum — TAR-1035.

La camara ANPR apunta su notification URL a la Pi (POST /anpr-event, multipart
con el XML EventNotificationAlert). Cada lectura se encola en SQLite (sobrevive
reinicios y cortes de internet) y un thread la reenvia EN ORDEN a Aditum
(POST {api}/aditum-gate/anpr-events, endpoint de TAR-1036) con el token de
dispositivo. El backend deduplica por eventUid, asi que reintentar tras un
timeout nunca duplica bitacora.

Decisiones clave:
  - eventUid: se usa el <UUID> que emite la propia camara cuando viene (estable
    ante reintentos del lado camara); si falta, uuid4 generado aqui. Siempre
    minusculas hex+guiones → seguro ante el UNIQUE case-insensitive de MySQL.
  - capturedAt: el <dateTime> del evento (ISO 8601 con offset) tal cual; si no
    parsea, la hora local de la Pi al recibirlo. El backend guarda ademas su
    propio receivedAt, por si el reloj de la Pi derrapo en un corte largo.
  - Reenvio uno-por-uno en orden de llegada (id autoincremental) con backoff
    5s → 5min. Sin token de dispositivo no se reenvia (quedan encolados).
  - Un 400 de Aditum es TERMINAL: el payload nunca va a ser aceptado (placa
    ilegible, fecha impresentable...). Se marca 'failed' y se sigue con el
    siguiente. Reintentarlo seria bloquear la cola ENTERA para siempre: el
    reenvio es estrictamente en orden, asi que la fila mas vieja tranca a
    todas las de atras. Todo lo demas (401 por rotacion de token, 5xx, red
    caida) SI se reintenta indefinidamente: un evento valido no se pierde.
  - Purga: los eventos ya confirmados por Aditum se borran despues de
    `purgeDays` dias, o al instante si `purgeDays` es 0 (unica excepcion al
    no-DELETE: es una cola, no historial). Lo PENDIENTE no se borra nunca:
    se reintenta hasta que Aditum lo confirme.
"""
import logging
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from . import httpclient
from .settings import ANPR_EVENTS_DB_FILE

log = logging.getLogger("aditum.anpr.events")

FORWARD_TIMEOUT = (3, 10)
BACKOFF_INITIAL = 5      # segundos
BACKOFF_MAX = 300        # 5 minutos
IDLE_POLL = 2            # segundos entre chequeos cuando no hay pendientes
PURGE_INTERVAL = 3600    # purga como maximo una vez por hora

_SCHEMA = """
CREATE TABLE IF NOT EXISTS anpr_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid     TEXT NOT NULL UNIQUE,
    license_plate TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    confidence    INTEGER,
    camera_name   TEXT,
    source_ip     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_anpr_event_status ON anpr_event(status, id);
"""


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _text_of(root, name):
    for e in root.iter():
        if _local(e.tag) == name and e.text and e.text.strip():
            return e.text.strip()
    return None


def parse_event_xml(xml_bytes):
    """Extrae los campos del EventNotificationAlert de la camara.

    Devuelve dict o None si el XML no es un evento ANPR con placa (los
    heartbeats/videoloss que mandan los alarm hosts se ignoran en silencio).
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    event_type = (_text_of(root, "eventType") or "").upper()
    if event_type != "ANPR":
        return None

    plate = _text_of(root, "licensePlate") or _text_of(root, "originalLicensePlate")
    if not plate:
        return None

    camera_uuid = (_text_of(root, "UUID") or "").strip().lower()
    confidence = None
    raw_confidence = _text_of(root, "confidenceLevel")
    if raw_confidence and raw_confidence.isdigit():
        confidence = int(raw_confidence)

    captured_at = _text_of(root, "dateTime")
    if captured_at:
        try:
            datetime.fromisoformat(captured_at)
        except ValueError:
            captured_at = None
    if not captured_at:
        captured_at = datetime.now().astimezone().isoformat(timespec="seconds")

    camera_name = (_text_of(root, "channelName") or _text_of(root, "deviceID")
                   or _text_of(root, "ipAddress") or "")

    return {
        "eventUid": camera_uuid or str(uuid.uuid4()),
        "licensePlate": plate,
        "capturedAt": captured_at,
        "confidenceLevel": confidence,
        "cameraName": camera_name,
    }


class AnprEventStore:
    """Cola persistente en SQLite (WAL: escrituras del receptor y lecturas
    del forwarder conviven sin bloquearse)."""

    def __init__(self, path=ANPR_EVENTS_DB_FILE):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(self, event, source_ip=None):
        """Inserta el evento; devuelve True si es nuevo, False si el
        event_uid ya estaba (reintento de la camara → idempotente)."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO anpr_event "
                "(event_uid, license_plate, captured_at, received_at,"
                " confidence, camera_name, source_ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event["eventUid"], event["licensePlate"], event["capturedAt"],
                 datetime.now().astimezone().isoformat(timespec="seconds"),
                 event["confidenceLevel"], event["cameraName"], source_ip))
            return cursor.rowcount == 1

    def next_pending(self):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM anpr_event WHERE status='pending' "
                "ORDER BY id LIMIT 1").fetchone()
            return dict(row) if row else None

    def mark_sent(self, event_id):
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE anpr_event SET status='sent', sent_at=?, last_error=NULL "
                "WHERE id=?",
                (datetime.now().astimezone().isoformat(timespec="seconds"), event_id))

    def delete(self, event_id):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM anpr_event WHERE id=?", (event_id,))

    def mark_failed(self, event_id, error):
        """Terminal: Aditum lo rechazo definitivamente. Sale de la cola de
        pendientes para no tapar a los que vienen atras, pero NO se borra:
        queda como evidencia para diagnosticar."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE anpr_event SET status='failed', attempts=attempts+1, "
                "last_error=? WHERE id=?", (str(error)[:200], event_id))

    def mark_attempt(self, event_id, error):
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE anpr_event SET attempts=attempts+1, last_error=? WHERE id=?",
                (str(error)[:200], event_id))

    def purge_sent(self, days):
        cutoff = (datetime.now().astimezone() - timedelta(days=days)) \
            .isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM anpr_event WHERE status='sent' AND sent_at < ?",
                (cutoff,))
            if cursor.rowcount:
                log.info("Purga ANPR: %s eventos confirmados eliminados",
                         cursor.rowcount)
            return cursor.rowcount

    def stats(self):
        with self._lock, self._connect() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM anpr_event WHERE status='pending'"
            ).fetchone()[0]
            sent = conn.execute(
                "SELECT COUNT(*) FROM anpr_event WHERE status='sent'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM anpr_event WHERE status='failed'"
            ).fetchone()[0]
            last = conn.execute(
                "SELECT event_uid, license_plate, captured_at, status, attempts,"
                " last_error, source_ip FROM anpr_event "
                "ORDER BY id DESC LIMIT 5").fetchall()
            return {
                "pending": pending,
                "sent": sent,
                "failed": failed,
                "recent": [dict(r) for r in last],
            }


class AnprEventForwarder(threading.Thread):
    """Reenvia la cola a Aditum en orden, con backoff ante fallos."""

    def __init__(self, settings, store):
        super().__init__(name="anpr-forwarder", daemon=True)
        self.settings = settings
        self.store = store
        self._backoff = BACKOFF_INITIAL
        self._last_purge = 0.0

    def _events_url(self):
        return f"{self.settings.api_base_url}/aditum-gate/anpr-events"

    def _forward(self, row):
        """Devuelve (ok, error, permanent). permanent=True => no reintentar."""
        # El token se lee por evento: PUT /token lo rota en memoria.
        token = self.settings.device_token
        if not token:
            return False, "sin token de dispositivo", False
        payload = {
            "eventUid": row["event_uid"],
            "licensePlate": row["license_plate"],
            "capturedAt": row["captured_at"],
            "confidenceLevel": row["confidence"],
            "cameraName": row["camera_name"],
        }
        try:
            resp = httpclient.request(
                "POST", self._events_url(), json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=FORWARD_TIMEOUT)
        except Exception as e:  # red caida: se reintenta con backoff
            return False, str(e)[:120], False
        if resp.ok:
            # 200 cubre RECORDED y DUPLICATE: los dos son exito. Un duplicado
            # significa que Aditum ya lo tenia (reintento tras timeout).
            return True, None, False
        if resp.status_code == 400:
            # Aditum explica el motivo con un enum corto y seguro de guardar
            # (MISSING_LICENSE_PLATE, UNPARSEABLE_CAPTURED_AT...). No se guarda
            # el cuerpo completo.
            reason = ""
            try:
                reason = (resp.json() or {}).get("reason") or ""
            except ValueError:
                pass
            return False, f"http_400 {reason}".strip(), True
        return False, f"http_{resp.status_code}", False

    def run(self):
        log.info("Forwarder ANPR iniciado → %s", self._events_url())
        while True:
            self._maybe_purge()
            row = self.store.next_pending()
            if row is None:
                time.sleep(IDLE_POLL)
                continue
            ok, error, permanent = self._forward(row)
            if not ok and permanent:
                # Descartar y SEGUIR: no dormir ni aplicar backoff, el proximo
                # evento no tiene la culpa del payload de este.
                self.store.mark_failed(row["id"], error)
                log.error("Evento ANPR %s DESCARTADO por Aditum (%s): placa %s "
                          "capturada %s. No se reintenta.",
                          row["event_uid"], error, row["license_plate"],
                          row["captured_at"])
                continue
            if ok:
                # Confirmado por Aditum: sale de la cola. Con purgeDays > 0 se
                # conserva un rato como evidencia (lo que muestra /anpr-status
                # y sirve para auditar un cutover); con 0 se borra al instante.
                if self.settings.anpr_purge_days == 0:
                    self.store.delete(row["id"])
                else:
                    self.store.mark_sent(row["id"])
                log.info("Evento ANPR %s reenviado (placa %s, capturado %s)",
                         row["event_uid"], row["license_plate"],
                         row["captured_at"])
                self._backoff = BACKOFF_INITIAL
                continue
            self.store.mark_attempt(row["id"], error)
            log.warning("Reenvio ANPR %s fallo (%s); reintento en %ss",
                        row["event_uid"], error, self._backoff)
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def _maybe_purge(self):
        now = time.monotonic()
        if now - self._last_purge >= PURGE_INTERVAL:
            self._last_purge = now
            try:
                self.store.purge_sent(self.settings.anpr_purge_days)
            except Exception:
                log.exception("Fallo la purga de eventos ANPR")
