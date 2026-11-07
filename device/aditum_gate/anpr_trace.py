"""Historial de placas ANPR en el Pi (trazabilidad).

Responde con evidencia lo que antes se perdia: cuando una placa ENTRO a la
lista de una camara, cuando SALIO, cada vez que la camara la LEYO (en que
lista la reporto) y que eventos de IO hubo alrededor. Con eso el endpoint
/anpr-plate-history (anpr_history.py) puede decir si una placa leida como
otherList estaba o no en el allowlist en ese momento.

De donde sale cada dato:
  - Entradas/salidas: cada escritura en la camara (/sync-plates,
    /update-plate, /anpr-test ADD/DELETE) ya exporta la lista ANTES de
    escribir. Ese "antes" contra el "despues" da las placas que entraron y
    salieron (source SYNC/UPDATE/TEST). Si el "antes" no coincide con lo que
    el Pi sabia de esa camara, alguien la toco por fuera (EXTERNAL). La
    primera escritura sobre una camara sin historial registra su lista
    completa como BASELINE.
  - Lecturas: todo lo que llega a /anpr-event (allowlist, otherList,
    blockList e ilegibles), no solo lo que se reenvia a Aditum.
  - IO: eventos IO que la camara notifique (ej. un sensor de la barrera en
    su ALARM IN). Es la unica forma de confirmar una apertura fisica.

Todos los timestamps se guardan dos veces: ISO local (para mostrar) y epoch
(`*_ts`, para comparar). Las entradas/salidas y `received_ts` usan el reloj
del Pi, asi que la membresia se evalua con `received_ts`; `captured_at` es
el reloj de la camara y puede estar corrido.

Nunca guarda credenciales: solo IP de camara, cameraId de Aditum y placas.
"""
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from .anpr import normalize_plate
from .settings import ANPR_EVENTS_DB_FILE

log = logging.getLogger("aditum.anpr.trace")

SOURCE_SYNC = "SYNC"
SOURCE_UPDATE = "UPDATE"
SOURCE_TEST = "TEST"
SOURCE_BASELINE = "BASELINE"
SOURCE_EXTERNAL = "EXTERNAL"

ADDED = "ADDED"
REMOVED = "REMOVED"

OUTCOME_QUEUED = "queued"
OUTCOME_DISCARDED = "discarded"
OUTCOME_UNREADABLE = "unreadable"

# Tope de filas que devuelve una consulta: protege a un equipo modesto de una
# respuesta enorme (una placa normal tiene decenas de lecturas al mes).
QUERY_LIMIT = 2000
# Candidatas para "lecturas parecidas": lecturas de la calle en el rango.
SIMILAR_SCAN_LIMIT = 20000

_SCHEMA = """
-- Camaras sobre las que el Pi escribio alguna vez. first_seen_ts marca
-- desde cuando el historial de su lista es confiable (antes: desconocido).
-- No se purga: es una fila por camara.
CREATE TABLE IF NOT EXISTS anpr_trace_camera (
    camera_ip      TEXT PRIMARY KEY,
    camera_id      TEXT,
    first_seen_at  TEXT NOT NULL,
    first_seen_ts  REAL NOT NULL,
    last_write_at  TEXT,
    last_write_ts  REAL
);

-- Cada escritura (o intento) sobre la lista de una camara.
CREATE TABLE IF NOT EXISTS anpr_sync_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    ts           REAL NOT NULL,
    camera_ip    TEXT NOT NULL,
    camera_id    TEXT,
    request_id   TEXT,
    kind         TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    error_code   TEXT,
    plate_count  INTEGER,
    added        INTEGER,
    removed      INTEGER,
    external     INTEGER,
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_anpr_sync_ip_ts ON anpr_sync_log(camera_ip, ts);

-- Una fila por placa que entra o sale de la lista de una camara.
CREATE TABLE IF NOT EXISTS anpr_plate_change (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    ts          REAL NOT NULL,
    camera_ip   TEXT NOT NULL,
    camera_id   TEXT,
    plate       TEXT NOT NULL,
    change      TEXT NOT NULL,
    source      TEXT NOT NULL,
    request_id  TEXT,
    sync_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_anpr_change_plate ON anpr_plate_change(plate, camera_ip, ts);
CREATE INDEX IF NOT EXISTS idx_anpr_change_ip ON anpr_plate_change(camera_ip, ts);

-- Cada lectura que la camara posteo a /anpr-event.
CREATE TABLE IF NOT EXISTS anpr_read_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid     TEXT UNIQUE,
    plate         TEXT,
    plate_raw     TEXT,
    captured_at   TEXT,
    captured_ts   REAL,
    received_at   TEXT NOT NULL,
    received_ts   REAL NOT NULL,
    camera_ip     TEXT,
    camera_name   TEXT,
    vehicle_list  TEXT,
    confidence    INTEGER,
    direction     TEXT,
    outcome       TEXT NOT NULL,
    capture_name  TEXT,
    photos        TEXT
);
CREATE INDEX IF NOT EXISTS idx_anpr_read_plate ON anpr_read_log(plate, received_ts);
CREATE INDEX IF NOT EXISTS idx_anpr_read_ip ON anpr_read_log(camera_ip, received_ts);

-- Eventos IO de la camara (alarm input/output), base para confirmar aperturas.
CREATE TABLE IF NOT EXISTS anpr_io_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_ip    TEXT,
    event_type   TEXT,
    port         TEXT,
    state        TEXT,
    captured_at  TEXT,
    received_at  TEXT NOT NULL,
    received_ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anpr_io_ip ON anpr_io_event(camera_ip, received_ts);
"""


def _now():
    now = datetime.now().astimezone()
    return now.isoformat(timespec="seconds"), now.timestamp()


def iso_to_ts(value):
    """ISO 8601 -> epoch. Sin zona se asume la hora local del Pi. None si no
    parsea."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def ts_to_iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class AnprTraceStore:
    """Historial persistente en el mismo anpr-events.db (WAL) que la cola."""

    def __init__(self, path=ANPR_EVENTS_DB_FILE, retention_days=90):
        self.path = str(path)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        """Conexion corta: commit (o rollback) y cierre al salir."""
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Escritura: lista de la camara
    # ------------------------------------------------------------------
    @staticmethod
    def _known_list(conn, camera_ip):
        """Lo que el Pi sabe que hay en la lista de la camara: placas cuyo
        ultimo cambio es ADDED. None si nunca se escribio en esa camara."""
        seen = conn.execute(
            "SELECT 1 FROM anpr_trace_camera WHERE camera_ip=?",
            (camera_ip,)).fetchone()
        if seen is None:
            return None
        rows = conn.execute(
            "SELECT plate, change FROM anpr_plate_change c WHERE camera_ip=? "
            "AND id = (SELECT MAX(id) FROM anpr_plate_change "
            "          WHERE camera_ip=c.camera_ip AND plate=c.plate)",
            (camera_ip,)).fetchall()
        return {r["plate"] for r in rows if r["change"] == ADDED}

    def record_write(self, camera_ip, kind, source, before, after,
                     camera_id=None, request_id=None, duration_ms=None):
        """Registra una escritura EXITOSA (o un no-op) sobre la lista.

        `before` es la lista que tenia la camara justo antes (export) y
        `after` la que quedo. Devuelve {added, removed, external}."""
        before, after = set(before), set(after)
        at, ts = _now()
        camera_id = None if camera_id is None else str(camera_id)
        with self._lock, self._connect() as conn:
            known = self._known_list(conn, camera_ip)
            changes = []
            if known is None:
                changes += [(p, ADDED, SOURCE_BASELINE) for p in sorted(before)]
                conn.execute(
                    "INSERT INTO anpr_trace_camera "
                    "(camera_ip, camera_id, first_seen_at, first_seen_ts) "
                    "VALUES (?, ?, ?, ?)", (camera_ip, camera_id, at, ts))
                external = 0
            else:
                ext_added = before - known
                ext_removed = known - before
                external = len(ext_added) + len(ext_removed)
                changes += [(p, ADDED, SOURCE_EXTERNAL) for p in sorted(ext_added)]
                changes += [(p, REMOVED, SOURCE_EXTERNAL) for p in sorted(ext_removed)]
            added, removed = after - before, before - after
            changes += [(p, ADDED, source) for p in sorted(added)]
            changes += [(p, REMOVED, source) for p in sorted(removed)]
            cursor = conn.execute(
                "INSERT INTO anpr_sync_log (at, ts, camera_ip, camera_id, "
                "request_id, kind, ok, plate_count, added, removed, external,"
                " duration_ms) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (at, ts, camera_ip, camera_id, _str(request_id), kind,
                 len(after), len(added), len(removed), external, duration_ms))
            sync_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO anpr_plate_change (at, ts, camera_ip, camera_id,"
                " plate, change, source, request_id, sync_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(at, ts, camera_ip, camera_id, plate, change, src,
                  _str(request_id), sync_id) for plate, change, src in changes])
            conn.execute(
                "UPDATE anpr_trace_camera SET last_write_at=?, last_write_ts=?,"
                " camera_id=COALESCE(?, camera_id) WHERE camera_ip=?",
                (at, ts, camera_id, camera_ip))
        if external:
            log.warning("Lista ANPR de %s cambiada por fuera del Pi: %s placa(s) "
                        "distintas a lo ultimo que escribio", camera_ip, external)
        return {"added": len(added), "removed": len(removed), "external": external}

    def record_write_error(self, camera_ip, kind, error_code, camera_id=None,
                           request_id=None, duration_ms=None):
        """Registra una escritura FALLIDA (la camara no cambio)."""
        at, ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO anpr_sync_log (at, ts, camera_ip, camera_id, "
                "request_id, kind, ok, error_code, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (at, ts, camera_ip, _str(camera_id), _str(request_id), kind,
                 error_code, duration_ms))

    # ------------------------------------------------------------------
    # Escritura: lecturas e IO
    # ------------------------------------------------------------------
    def record_read(self, event, source_ip, outcome, capture=None):
        """Registra una lectura de /anpr-event. `capture` es el registro que
        devolvio AnprCaptureStore.save (para enlazar las fotos)."""
        at, ts = _now()
        plate_raw = event.get("licensePlate")
        photos = ",".join(capture.get("images") or []) if capture else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO anpr_read_log (event_uid, plate, plate_raw,"
                " captured_at, captured_ts, received_at, received_ts, camera_ip,"
                " camera_name, vehicle_list, confidence, direction, outcome,"
                " capture_name, photos) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.get("eventUid"), normalize_plate(plate_raw), plate_raw,
                 event.get("capturedAt"), iso_to_ts(event.get("capturedAt")),
                 at, ts, source_ip, event.get("cameraName"),
                 event.get("vehicleList", ""), event.get("confidenceLevel"),
                 event.get("direction"), outcome,
                 capture.get("name") if capture else None, photos))

    def record_io(self, io_event, source_ip):
        at, ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO anpr_io_event (camera_ip, event_type, port, state,"
                " captured_at, received_at, received_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_ip, io_event.get("eventType"), io_event.get("port"),
                 io_event.get("state"), io_event.get("capturedAt"), at, ts))

    # ------------------------------------------------------------------
    # Purga
    # ------------------------------------------------------------------
    def purge(self, days=None):
        """Borra lo mas viejo que `days`. De los cambios de lista conserva,
        por placa y camara, el ultimo anterior al corte: es el estado de la
        lista en ese momento y sin el la membresia posterior saldria mal."""
        days = self.retention_days if days is None else days
        cutoff = time.time() - days * 86400
        with self._lock, self._connect() as conn:
            removed = 0
            for table, column in (("anpr_read_log", "received_ts"),
                                  ("anpr_io_event", "received_ts"),
                                  ("anpr_sync_log", "ts")):
                removed += conn.execute(
                    f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)).rowcount
            removed += conn.execute(
                "DELETE FROM anpr_plate_change WHERE ts < ? AND id NOT IN ("
                " SELECT MAX(id) FROM anpr_plate_change WHERE ts < ?"
                " GROUP BY camera_ip, plate)", (cutoff, cutoff)).rowcount
        if removed:
            log.info("Purga del historial ANPR: %s fila(s) de mas de %s dias",
                     removed, days)
        return removed

    # ------------------------------------------------------------------
    # Consultas (para anpr_history)
    # ------------------------------------------------------------------
    def cameras(self):
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM anpr_trace_camera ORDER BY camera_ip")]

    def camera_ips_for_id(self, camera_id):
        """IPs que uso el cameraId de Aditum (la mas reciente primero)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT camera_ip, MAX(ts) AS last FROM anpr_sync_log "
                "WHERE camera_id=? GROUP BY camera_ip ORDER BY last DESC",
                (str(camera_id),)).fetchall()
            return [r["camera_ip"] for r in rows]

    def plate_changes(self, plate, camera_ips=None, until_ts=None):
        sql = "SELECT * FROM anpr_plate_change WHERE plate=?"
        args = [plate]
        sql, args = _filter_ips(sql, args, camera_ips)
        if until_ts is not None:
            sql += " AND ts <= ?"
            args.append(until_ts)
        sql += " ORDER BY ts, id LIMIT ?"
        args.append(QUERY_LIMIT)
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def sync_log(self, camera_ips=None, since_ts=None, until_ts=None, only_errors=False):
        sql = "SELECT * FROM anpr_sync_log WHERE 1=1"
        args = []
        sql, args = _filter_ips(sql, args, camera_ips)
        sql, args = _filter_range(sql, args, "ts", since_ts, until_ts)
        if only_errors:
            sql += " AND ok=0"
        sql += " ORDER BY ts, id LIMIT ?"
        args.append(QUERY_LIMIT)
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def reads(self, plate, camera_ips=None, since_ts=None, until_ts=None):
        sql = "SELECT * FROM anpr_read_log WHERE plate=?"
        args = [plate]
        sql, args = _filter_ips(sql, args, camera_ips)
        sql, args = _filter_range(sql, args, "received_ts", since_ts, until_ts)
        sql += " ORDER BY received_ts, id LIMIT ?"
        args.append(QUERY_LIMIT)
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def similar_candidates(self, plate, camera_ips=None, since_ts=None, until_ts=None):
        """Lecturas de OTRAS placas de largo parecido (posible mala lectura)."""
        sql = ("SELECT * FROM anpr_read_log WHERE plate IS NOT NULL AND plate<>? "
               "AND length(plate) BETWEEN ? AND ?")
        args = [plate, len(plate) - 1, len(plate) + 1]
        sql, args = _filter_ips(sql, args, camera_ips)
        sql, args = _filter_range(sql, args, "received_ts", since_ts, until_ts)
        sql += " ORDER BY received_ts DESC LIMIT ?"
        args.append(SIMILAR_SCAN_LIMIT)
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def io_events(self, camera_ips=None, since_ts=None, until_ts=None):
        sql = "SELECT * FROM anpr_io_event WHERE 1=1"
        args = []
        sql, args = _filter_ips(sql, args, camera_ips)
        sql, args = _filter_range(sql, args, "received_ts", since_ts, until_ts)
        sql += " ORDER BY received_ts LIMIT ?"
        args.append(QUERY_LIMIT)
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def membership_at(self, camera_ip, plate, ts):
        """(estado, cambio) de `plate` en la lista de `camera_ip` en `ts`.

        estado: True (estaba), False (no estaba) o None (no hay historial de
        esa camara para ese momento). `cambio` es la fila que lo determina."""
        with self._lock, self._connect() as conn:
            camera = conn.execute(
                "SELECT first_seen_ts FROM anpr_trace_camera WHERE camera_ip=?",
                (camera_ip,)).fetchone()
            if camera is None or ts < camera["first_seen_ts"]:
                return None, None
            row = conn.execute(
                "SELECT * FROM anpr_plate_change WHERE camera_ip=? AND plate=? "
                "AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
                (camera_ip, plate, ts)).fetchone()
            if row is None:
                return False, None
            return row["change"] == ADDED, dict(row)


def _str(value):
    return None if value is None else str(value)


def _filter_ips(sql, args, camera_ips):
    if camera_ips:
        sql += " AND camera_ip IN (%s)" % ",".join("?" * len(camera_ips))
        args = list(args) + list(camera_ips)
    return sql, args


def _filter_range(sql, args, column, since_ts, until_ts):
    args = list(args)
    if since_ts is not None:
        sql += f" AND {column} >= ?"
        args.append(since_ts)
    if until_ts is not None:
        sql += f" AND {column} <= ?"
        args.append(until_ts)
    return sql, args
