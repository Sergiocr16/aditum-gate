"""Receptor local de eventos ANPR + cola offline + reenvio a Aditum — TAR-1035.

La camara ANPR apunta su notification URL a la Pi (POST /anpr-event, multipart
con el XML EventNotificationAlert). Cada lectura se encola en SQLite (sobrevive
reinicios y cortes de internet) y un thread la reenvia EN ORDEN a Aditum
(POST {api}/aditum-gate/anpr-events, endpoint de TAR-1036) con el token de
dispositivo. El backend deduplica por eventUid, asi que reintentar tras un
timeout nunca duplica bitacora.

Decisiones clave:
  - Solo se encolan lecturas del ALLOW LIST: el evento trae <vehicleListName>
    y se descartan blackList/otherList (y las placas 'unknown' ilegibles). El
    gate lo abre la camara localmente; la bitacora es solo de autorizadas.
    OJO: el allow list se llama "whiteList" en firmwares viejos y "allowList"
    en los nuevos; ver ALLOW_LIST_NAMES.
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
    reenvio es en orden, asi que la fila mas vieja tranca a todas las de
    atras. Todo lo demas (401 por rotacion de token, 5xx, red caida) SI se
    reintenta indefinidamente: un evento valido no se pierde.
  - Pero reintentar para siempre tampoco puede congelar la cola: tras
    DEFER_AFTER intentos fallidos el evento cede el turno DEFER_SECONDS
    (columna retry_after) y el forwarder sigue con el siguiente. No se
    descarta —vuelve a la fila al vencer— a costa de que el orden de entrega
    deje de ser estricto. Aditum lo tolera: deduplica por eventUid y el
    capturedAt de cada lectura viaja en el payload.
  - De cada fallo se guarda el codigo HTTP mas un recorte del cuerpo de la
    respuesta en last_error. Un 'http_500' a secas no deja ninguna pista de
    que fue lo que Aditum rechazo.
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
DEFER_AFTER = 5          # intentos fallidos antes de dejar pasar a los de atras
DEFER_SECONDS = 600      # 10 min que el evento pospuesto cede el paso
ERROR_BODY_CHARS = 150   # cuanto del cuerpo del error se guarda en last_error

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
    sent_at       TEXT,
    retry_after   TEXT,
    vehicle_list  TEXT
);
CREATE INDEX IF NOT EXISTS idx_anpr_event_status ON anpr_event(status, id);

-- Cuantas lecturas llegaron con cada <vehicleListName> y que se hizo con
-- ellas. Las descartadas tambien se guardan como filas (status='discarded'),
-- pero la purga se las lleva a los purgeDays dias: este contador es
-- ACUMULATIVO y sobrevive a la purga, asi que sigue siendo lo que permite
-- distinguir "no habia nada que descartar" de "la camara no reporta la lista
-- y el filtro no descarta nunca" y de "el firmware usa otro nombre y se
-- descarta todo" cuando la ventana de filas ya se vacio. No guarda placas:
-- solo el nombre de la lista.
CREATE TABLE IF NOT EXISTS anpr_list_stat (
    vehicle_list TEXT PRIMARY KEY,
    queued       INTEGER NOT NULL DEFAULT 0,
    discarded    INTEGER NOT NULL DEFAULT 0,
    last_at      TEXT
);
"""


def _now():
    """Ahora en ISO 8601 local. Todos los timestamps de la tabla se generan
    igual, asi que compararlos como texto ordena bien."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _text_of(root, name):
    for e in root.iter():
        if _local(e.tag) == name and e.text and e.text.strip():
            return e.text.strip()
    return None


# La camara emite "unknown" como placa cuando el OCR no la pudo leer. Es una
# no-lectura (como un heartbeat): no se encola ni se reenvia a Aditum. Se
# compara en minusculas por si el firmware varia el casing.
_NO_PLATE_SENTINELS = {"unknown"}


# Nombres con los que la camara reporta SU allow list en <vehicleListName>.
# Hikvision cambio la terminologia en los firmwares nuevos (whiteList ->
# allowList, igual que blackList -> blockList) y hay equipos de las dos epocas
# en la flota: son la MISMA lista, la de placas autorizadas. Reconocer solo una
# hace que el filtro descarte en silencio todas las lecturas autorizadas.
ALLOW_LIST_NAMES = ("whitelist", "allowlist")


def is_authorized(event):
    """True si la lectura es del allow list (whiteList/allowList) o si la camara
    no reporto la lista ("" -> fallback: no descartar una autorizada por falta
    del dato). blackList/blockList/otherList => False."""
    return event.get("vehicleList", "") in ("",) + ALLOW_LIST_NAMES


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
    if not plate or plate.strip().lower() in _NO_PLATE_SENTINELS:
        return None

    # <vehicleListName>: resultado del match de la camara contra sus listas —
    # whiteList/allowList (autorizada), blackList/blockList (vetada) u otherList
    # (no esta en ninguna);
    # "" si el firmware no lo reporta. El filtro por allow list lo decide el
    # caller segun anpr.onlyAuthorized (ver is_authorized), no aca.
    vehicle_list = (_text_of(root, "vehicleListName") or "").strip().lower()

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
        "vehicleList": vehicle_list,
    }


class AnprEventStore:
    """Cola persistente en SQLite (WAL: escrituras del receptor y lecturas
    del forwarder conviven sin bloquearse)."""

    def __init__(self, path=ANPR_EVENTS_DB_FILE):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn):
        """Agrega columnas nuevas a las bases que ya existen en la flota.
        CREATE TABLE IF NOT EXISTS no toca una tabla ya creada."""
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(anpr_event)")}
        if "retry_after" not in columns:
            conn.execute("ALTER TABLE anpr_event ADD COLUMN retry_after TEXT")
        if "vehicle_list" not in columns:
            conn.execute("ALTER TABLE anpr_event ADD COLUMN vehicle_list TEXT")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _insert(self, event, source_ip, status):
        """Inserta el evento con el status dado; devuelve True si es nuevo,
        False si el event_uid ya estaba (reintento de la camara →
        idempotente)."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO anpr_event "
                "(event_uid, license_plate, captured_at, received_at,"
                " confidence, camera_name, source_ip, status, vehicle_list) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event["eventUid"], event["licensePlate"], event["capturedAt"],
                 _now(), event["confidenceLevel"], event["cameraName"],
                 source_ip, status, event.get("vehicleList", "")))
            return cursor.rowcount == 1

    def enqueue(self, event, source_ip=None):
        """Encola el evento para reenviarlo a Aditum."""
        return self._insert(event, source_ip, "pending")

    def record_discarded(self, event, source_ip=None):
        """Guarda una lectura que el filtro de allowlist descarto.

        Queda como fila 'discarded': NO se reenvia nunca (next_pending solo
        mira 'pending') y la purga la borra igual que a las confirmadas. Sirve
        para responder "por que esta placa no aparece en la bitacora" desde
        /admin, sin tener que entrar por SSH a leer el log."""
        return self._insert(event, source_ip, "discarded")

    def record_list_outcome(self, vehicle_list, discarded):
        """Suma una lectura al contador de su <vehicleListName>.

        `vehicle_list` es "" cuando la camara no reporta el dato — ese caso
        es justamente el que delata que el filtro no esta filtrando."""
        column = "discarded" if discarded else "queued"
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO anpr_list_stat (vehicle_list, {column}, last_at) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(vehicle_list) DO UPDATE SET "
                f"{column}={column}+1, last_at=excluded.last_at",
                (vehicle_list or "", _now()))

    def next_pending(self):
        """El pendiente mas viejo que no este pospuesto. Un evento que Aditum
        rechaza una y otra vez cede el turno (retry_after) para no trancar a
        los que vienen atras; sigue pendiente y vuelve a la fila al vencer."""
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM anpr_event WHERE status='pending' "
                "AND (retry_after IS NULL OR retry_after <= ?) "
                "ORDER BY id LIMIT 1", (now,)).fetchone()
            return dict(row) if row else None

    def mark_sent(self, event_id):
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE anpr_event SET status='sent', sent_at=?, last_error=NULL, "
                "retry_after=NULL WHERE id=?",
                (datetime.now().astimezone().isoformat(timespec="seconds"), event_id))

    def delete(self, event_id):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM anpr_event WHERE id=?", (event_id,))

    def delete_pending(self):
        """Borra todas las lecturas pendientes de enviar (no toca sent/failed).
        Devuelve cuantas borro. Para el boton del editor tras un cutover/pruebas."""
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM anpr_event WHERE status='pending'")
            return cur.rowcount

    def mark_failed(self, event_id, error):
        """Terminal: Aditum lo rechazo definitivamente. Sale de la cola de
        pendientes para no tapar a los que vienen atras, pero NO se borra:
        queda como evidencia para diagnosticar."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE anpr_event SET status='failed', attempts=attempts+1, "
                "last_error=? WHERE id=?", (str(error)[:200], event_id))

    def mark_attempt(self, event_id, error, defer_seconds=None):
        """Suma un intento. Con defer_seconds, ademas posterga el evento esos
        segundos para que la cola siga avanzando sin el."""
        retry_after = None
        if defer_seconds:
            retry_after = (datetime.now().astimezone()
                           + timedelta(seconds=defer_seconds)) \
                .isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE anpr_event SET attempts=attempts+1, last_error=?, "
                "retry_after=? WHERE id=?",
                (str(error)[:200], retry_after, event_id))

    def purge_processed(self, days):
        """Borra lo que ya no tiene nada pendiente que hacer: confirmadas por
        Aditum y descartadas por el filtro. Lo PENDIENTE no se toca nunca.

        Las descartadas no tienen sent_at, asi que su edad se mide por
        received_at."""
        cutoff = (datetime.now().astimezone() - timedelta(days=days)) \
            .isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM anpr_event WHERE status IN ('sent', 'discarded') "
                "AND COALESCE(sent_at, received_at) < ?", (cutoff,))
            if cursor.rowcount:
                log.info("Purga ANPR: %s eventos procesados eliminados "
                         "(confirmados + descartados)", cursor.rowcount)
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
            deferred = conn.execute(
                "SELECT COUNT(*) FROM anpr_event WHERE status='pending' "
                "AND retry_after > ?", (_now(),)).fetchone()[0]
            lists = [dict(r) for r in conn.execute(
                "SELECT vehicle_list, queued, discarded, last_at "
                "FROM anpr_list_stat ORDER BY queued + discarded DESC")]
            # La cola y los descartes van por separado: si se mezclaran, el
            # ruido de la calle (otherList) taparia las lecturas que importan.
            last = conn.execute(
                "SELECT event_uid, license_plate, captured_at, status, attempts,"
                " last_error, source_ip, retry_after, vehicle_list "
                "FROM anpr_event WHERE status <> 'discarded' "
                "ORDER BY id DESC LIMIT 5").fetchall()
            last_discarded = conn.execute(
                "SELECT license_plate, captured_at, received_at, source_ip,"
                " vehicle_list FROM anpr_event WHERE status = 'discarded' "
                "ORDER BY id DESC LIMIT 10").fetchall()
            discarded_stored = conn.execute(
                "SELECT COUNT(*) FROM anpr_event WHERE status='discarded'"
            ).fetchone()[0]
            return {
                "pending": pending,
                "sent": sent,
                "failed": failed,
                "deferred": deferred,
                # 'discarded' es el acumulado historico del contador; se
                # mantiene aunque la purga ya se haya llevado las filas.
                "discarded": sum(r["discarded"] for r in lists),
                "discardedStored": discarded_stored,
                "lists": lists,
                "recent": [dict(r) for r in last],
                "recentDiscarded": [dict(r) for r in last_discarded],
            }


def _response_snippet(resp):
    """Una linea del cuerpo de la respuesta, recortada, para el last_error."""
    try:
        body = (resp.text or "").strip()
    except Exception:
        return ""
    return " ".join(body.split())[:ERROR_BODY_CHARS]


class AnprEventForwarder(threading.Thread):
    """Reenvia la cola a Aditum en orden de llegada, con backoff ante fallos.
    El evento que falla DEFER_AFTER veces cede el turno para que la cola no
    quede congelada detras de el."""

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
            return False, f"http_400 {reason or _response_snippet(resp)}".strip(), True
        return False, f"http_{resp.status_code} {_response_snippet(resp)}".strip(), False

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
            attempts = row["attempts"] + 1
            if attempts >= DEFER_AFTER:
                # Deja pasar a los de atras: un solo evento atascado no puede
                # congelar la bitacora entera del condominio.
                self.store.mark_attempt(row["id"], error,
                                        defer_seconds=DEFER_SECONDS)
                log.error("Evento ANPR %s lleva %s intentos fallidos (%s): se "
                          "pospone %ss y sigue la cola. NO se descarta.",
                          row["event_uid"], attempts, error, DEFER_SECONDS)
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
                self.store.purge_processed(self.settings.anpr_purge_days)
            except Exception:
                log.exception("Fallo la purga de eventos ANPR")
