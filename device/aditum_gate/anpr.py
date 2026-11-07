"""Lista de placas ANPR en camaras Hikvision (ISAPI) — TAR-1034.

El Pi actua de puente igual que con las tarjetas QR (hikvision.py): Aditum
hace POST /update-plate (placa individual) o POST /sync-plates (reemplazo
completo) y aqui se aplica sobre la lista blanca que vive DENTRO de la
camara ANPR, para que autorice y abra sola sin internet.

Contrato con el backend (CONTRACT.md de TAR-1033 en aditum-jh):
  - El backend NO parsea el body de la respuesta: solo mira el codigo HTTP.
    2xx = exito; todo lo demas es fallo reintentable. El body igual se emite
    segun el contrato para debugging y para un TAR futuro que lo parsee.
  - La IDENTIDAD de la placa es plateNormalized ([A-Z0-9]+): es lo que se
    guarda en la camara (el OCR de la camara lee sin separadores). La placa
    cruda es solo informativa.
  - Idempotencia obligatoria: ADD de una placa existente y DELETE de una
    inexistente responden 200 con detail *_noop.
  - La Pi es stateless: las credenciales de camara llegan en cada request y
    se descartan. JAMAS loguear el request completo ni credenciales; solo
    requestId, action, cameraId, plateNormalized e ip.

ISAPI usado (guia oficial "ANPR Camera Integration Solution for 7 series"):
  - GET/PUT /ISAPI/Traffic/channels/<ch>/licensePlateAuditData?fileType=csv
      exporta/importa la lista completa como archivo CSV (UTF-8 con BOM, CRLF;
      columnas No.,Placa,Grupo,FechaInicio,FechaFin,CardID). Es un import por
      ARCHIVO ("Opaque Data" en la doc), NO un PUT de XML: el firmware rechaza
      con "Device Error" un XML de lista. Se hace read-modify-write para la
      placa individual y replace para el full sync. El import responde
      "overLimit" = lista llena, que se reporta explicito (LIST_FULL).
  - GET /ISAPI/Traffic/capabilities → plateListNum (capacidad maxima).

NOTA timeouts: el backend corta la conexion a los 5 s (deviceRestTemplate).
Por eso aqui NO se usa la sesion compartida de httpclient (trae reintentos
automaticos que multiplican la latencia): cada llamada ISAPI va directa con
timeouts cortos y un solo intento — si la camara no responde, se devuelve
504 rapido y el backend reintenta en su proximo ciclo. Excepcion: el full
sync (/sync-plates) es bulk y usa ISAPI_SYNC_TIMEOUT mas largo; eso exige que
el backend suba su read-timeout para ese endpoint en tandem (ver ese constante).
"""
import csv
import io
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from requests.auth import HTTPDigestAuth

log = logging.getLogger("aditum.anpr")

# (connect, read) para las llamadas ISAPI que NO dependen del tamano de la
# lista (capabilities). Cabe en el budget de 5 s del backend en LAN sana.
ISAPI_TIMEOUT = (2, 2.5)
# TODO lo que toca la lista de placas (export GET e import PUT) escala con su
# tamano, sea full sync, alta suelta o diagnostico: la camara genera el CSV
# completo antes de mandar el primer byte. Medido: ~1.6-2.3 ms/placa el export
# (una iDS-2CD7A46G0 con 2656 placas tarda 6-7 s en empezar a responder) y
# ~3.5 ms/placa el import. Con el timeout corto una camara con >~1000 placas
# NUNCA pasaria el probe ni un update-plate, asi que esas llamadas usan este.
# Export: 30 s de lectura cubren la capacidad maxima de la camara (10000
# placas a ~2.3 ms). connect corto igual: camara caida se corta rapido.
ISAPI_SYNC_TIMEOUT = (3, 30)
# Import: escala con las filas que se mandan. Un sync de 2655 placas contra la
# iDS-2CD7A46G0 NO entraba en los 12 s fijos que habia antes (CAMERA_TIMEOUT
# a los ~18 s de request), asi que se da 10 ms/placa (~2x lo medido) con piso
# de 12 s. El Pi NUNCA debe rendirse antes que el backend: el read-timeout que
# aditum-jh use para /sync-plates tiene que quedar por ENCIMA de export +
# import de su lista mas grande (con 2655 placas, >= 45 s). Para /update-plate
# el backend corta a los 5 s: contra una lista grande su primer intento vence,
# pero el Pi termina la escritura igual y el reintento del backend cae en el
# no-op idempotente (already_present_noop / not_found_noop).
IMPORT_SECONDS_PER_PLATE = 0.010
IMPORT_READ_MIN, IMPORT_READ_MAX = 12, 120


def import_timeout(rows):
    """(connect, read) para importar `rows` filas: crece con la lista."""
    read = 4 + len(rows) * IMPORT_SECONDS_PER_PLATE
    return (3, min(IMPORT_READ_MAX, max(IMPORT_READ_MIN, read)))

DEFAULT_CHANNEL = 1

# Grupo del CSV de Hikvision: 0 = lista negra, 1 = lista blanca (allowlist).
# El feature autoriza placas del condominio, asi que siempre se escribe "1".
ALLOW_LIST_GROUP = "1"
# Vigencia: las placas del condominio no expiran en la practica, pero la camara
# exige un rango de fechas (YYYY-MM-DD) y lo evalua contra SU PROPIO reloj.
# El inicio es una fecha fija en el pasado, nunca "hoy": con la fecha del Pi
# como inicio, una camara con el reloj atrasado (zona horaria mal puesta, sin
# NTP) ve las placas recien sincronizadas como "todavia no vigentes" y las
# reporta otherList aunque esten en la lista blanca, hasta que su reloj
# alcance esa fecha. Fin = hoy + estos anios.
VALIDITY_START = "2000-01-01"
VALIDITY_YEARS = 20

# Columnas del CSV export/import (orden fijo del header del firmware).
_COL_NO, _COL_PLATE, _COL_GROUP, _COL_START, _COL_END, _COL_CARD = range(6)

# Etiqueta legible del grupo del CSV, para que una consulta distinga una placa
# que esta pero en lista NEGRA de una que esta autorizada.
_GROUP_LABELS = {ALLOW_LIST_GROUP: "allowlist", "0": "blacklist"}

# Tope de placas que devuelve una consulta de lista. La camara admite miles
# (plateListNum); el tope es una valvula de seguridad para no armar una
# respuesta enorme en un equipo modesto, y se avisa con `truncated`.
PLATE_LIST_LIMIT = 5000

# Normalizacion espejo de LicensePlateUtil (aditum-jh): mayusculas + solo
# [A-Z0-9]. Se usa SOLO para comparar contra entradas legadas cargadas a
# mano en la camara; el valor que manda el backend ya viene normalizado y
# NO se re-transforma.
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def normalize_plate(plate):
    if plate is None:
        return None
    cleaned = _NON_ALNUM.sub("", plate).upper()
    return cleaned or None


def _local(tag):
    """Nombre local de un tag XML, ignorando namespace."""
    return tag.split("}")[-1] if "}" in tag else tag


class AnprCameraError(Exception):
    """Fallo clasificado contra una camara. `code` alimenta el detail del
    contrato y decide el HTTP hacia el backend (502 vs 504)."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code          # CAMERA_UNREACHABLE | CAMERA_TIMEOUT | CAMERA_AUTH | CAMERA_ERROR | LIST_FULL
        self.message = message


class AnprCameraClient:
    """Operaciones ISAPI de lista de placas contra UNA camara."""

    def __init__(self, channel=DEFAULT_CHANNEL):
        self.channel = channel
        # Un lock por camara: el import es un REEMPLAZO de la lista completa,
        # asi que dos read-modify-write solapados (el backend reintenta a los
        # 5 s mientras el primero sigue exportando) se pisarian y perderian
        # una placa. Se serializan por IP.
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _camera_lock(self, ip):
        with self._locks_guard:
            return self._locks.setdefault(ip, threading.Lock())

    @staticmethod
    def _auth(user, password):
        return HTTPDigestAuth(user, password)

    def _request(self, method, url, user, password, timeout=ISAPI_TIMEOUT, **kwargs):
        try:
            return requests.request(method, url, auth=self._auth(user, password),
                                    timeout=timeout, **kwargs)
        except requests.exceptions.ConnectTimeout:
            raise AnprCameraError("CAMERA_UNREACHABLE", "connect timeout")
        except requests.exceptions.ReadTimeout:
            raise AnprCameraError("CAMERA_TIMEOUT", "read timeout")
        except requests.exceptions.ConnectionError as e:
            raise AnprCameraError("CAMERA_UNREACHABLE", str(e)[:120])

    def isapi(self, method, ip, user, password, path, timeout=ISAPI_TIMEOUT, **kwargs):
        """Llamada ISAPI cruda (digest, un intento). Para el diagnostico de
        solo lectura (anpr_camera_diag); lanza AnprCameraError si no hay red."""
        return self._request(method, f"http://{ip}{path}", user, password,
                             timeout=timeout, **kwargs)

    def _list_url(self, ip):
        return (f"http://{ip}/ISAPI/Traffic/channels/{self.channel}"
                f"/licensePlateAuditData?fileType=csv")

    def get_plate_rows(self, ip, user, password, timeout=ISAPI_TIMEOUT):
        """Exporta la lista y la devuelve como (header, rows).

        `header` es la fila de titulos tal cual la emite el firmware (se reusa
        al reimportar, es device-dependant); `rows` es una lista de filas, cada
        una una lista de campos en el orden _COL_*. En el full sync el export
        tambien puede ser grande, por eso `timeout` es parametrizable."""
        resp = self._request("GET", self._list_url(ip), user, password, timeout=timeout)
        if resp.status_code == 401:
            raise AnprCameraError("CAMERA_AUTH", "digest auth rechazada")
        if resp.status_code != 200:
            raise AnprCameraError("CAMERA_ERROR", f"http_{resp.status_code} en export")
        # El firmware emite UTF-8 con BOM; utf-8-sig lo descarta.
        text = resp.content.decode("utf-8-sig", "replace")
        parsed = [row for row in csv.reader(io.StringIO(text)) if row]
        if not parsed:
            raise AnprCameraError("CAMERA_ERROR", "export CSV sin header")
        return parsed[0], parsed[1:]

    def put_plate_rows(self, ip, user, password, header, rows, timeout=ISAPI_TIMEOUT):
        """Importa (reemplaza) la lista completa desde filas CSV.

        Reconstruye el archivo con el mismo formato que exporta la camara
        (BOM + CRLF) y detecta overLimit explicito (lista llena)."""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
        body = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM que espera el firmware
        resp = self._request("PUT", self._list_url(ip), user, password,
                             timeout=timeout, data=body,
                             headers={"Content-Type": "text/csv"})
        if resp.status_code == 401:
            raise AnprCameraError("CAMERA_AUTH", "digest auth rechazada")
        text = resp.text or ""
        # El firmware responde ResponseStatus (ok) o ImportResult; "overLimit"
        # es el unico error que el proyecto exige distinguir (lista llena).
        if "overLimit" in text:
            raise AnprCameraError("LIST_FULL", "la camara reporto overLimit")
        if (resp.status_code != 200 or "importFail" in text
                or "importErrorData" in text or "<existError>true" in text):
            raise AnprCameraError("CAMERA_ERROR", f"http_{resp.status_code} en import")
        return True

    def plate_capacity(self, ip, user, password):
        """plateListNum reportado por la camara, o None si no lo expone.
        Best-effort: nunca lanza — la capacidad es un pre-chequeo opcional."""
        try:
            resp = self._request("GET", f"http://{ip}/ISAPI/Traffic/capabilities",
                                 user, password)
            if resp.status_code != 200:
                return None
            root = ET.fromstring(resp.content)
            for e in root.iter():
                if _local(e.tag) == "plateListNum" and (e.text or "").strip().isdigit():
                    return int(e.text.strip())
        except (AnprCameraError, ET.ParseError):
            return None
        return None

    # ------------------------------------------------------------------
    # Manipulacion de las filas CSV. Se preserva el header tal como lo
    # exporto el firmware y solo se agregan/quitan filas de datos.
    # ------------------------------------------------------------------

    @staticmethod
    def _row_plate(row):
        """Placa normalizada de una fila CSV, o None si la fila no la trae."""
        if len(row) <= _COL_PLATE:
            return None
        return normalize_plate(row[_COL_PLATE])

    @classmethod
    def _allow_set(cls, rows):
        """Placas de la lista que la camara trata como autorizadas (todo lo
        que no esta en el grupo de lista negra). Es lo que el historial
        (anpr_trace) registra como "esta en el allowlist"."""
        return {p for p in (cls._row_plate(r) for r in rows
                            if len(r) <= _COL_GROUP or r[_COL_GROUP].strip() != "0")
                if p}

    @staticmethod
    def _new_row(plate_normalized):
        """Fila nueva de allowlist para `plate_normalized`, vigente desde
        VALIDITY_START (fecha fija en el pasado, independiente del reloj de la
        camara) y hasta hoy + VALIDITY_YEARS (sin expiracion practica). El No.
        se renumera antes de importar."""
        today = datetime.now()
        try:
            end = today.replace(year=today.year + VALIDITY_YEARS)
        except ValueError:  # 29-feb en anio destino no bisiesto -> 28-feb
            end = today.replace(year=today.year + VALIDITY_YEARS, day=28)
        return ["", plate_normalized, ALLOW_LIST_GROUP,
                VALIDITY_START, end.strftime("%Y-%m-%d"), ""]

    @staticmethod
    def _renumber(rows):
        for i, row in enumerate(rows, start=1):
            row[_COL_NO] = str(i)

    def probe(self, ip, user, password):
        """Diagnostico de SOLO LECTURA: confirma IP, credenciales y soporte
        de lista de placas, sin modificar nada en la camara."""
        _header, rows = self.get_plate_rows(ip, user, password,
                                            timeout=ISAPI_SYNC_TIMEOUT)
        plates = [p for p in (self._row_plate(r) for r in rows) if p]
        return {
            "plates": len(plates),
            "capacity": self.plate_capacity(ip, user, password),
            "sample": plates[:5],
        }

    @classmethod
    def _row_detail(cls, row):
        """Grupo y vigencia de una fila del CSV, para poder responder no solo
        'esta' sino EN QUE lista esta y hasta cuando."""
        def field(index):
            return row[index].strip() if len(row) > index else ""
        group = field(_COL_GROUP)
        return {
            "plate": cls._row_plate(row),
            "group": _GROUP_LABELS.get(group, group),
            "validFrom": field(_COL_START),
            "validTo": field(_COL_END),
        }

    def list_plates(self, ip, user, password, limit=PLATE_LIST_LIMIT):
        """Lista de SOLO LECTURA de las placas cargadas en la camara.

        Las placas vuelven normalizadas, ordenadas y sin repetir (una lista
        cargada a mano puede tener duplicados); `total` es cuantas distintas
        hay en la camara, no cuantas se devuelven."""
        _header, rows = self.get_plate_rows(ip, user, password,
                                            timeout=ISAPI_SYNC_TIMEOUT)
        plates = sorted({p for p in (self._row_plate(r) for r in rows) if p})
        return {
            "total": len(plates),
            "plates": plates[:limit],
            "truncated": len(plates) > limit,
            "capacity": self.plate_capacity(ip, user, password),
        }

    def find_plate(self, ip, user, password, plate_normalized):
        """Busca UNA placa en la lista de la camara (SOLO LECTURA).

        No alcanza con found/not found: una placa puede estar cargada en la
        lista negra o con una vigencia vencida, y para el que consulta eso no
        es lo mismo que estar autorizada. Por eso vuelve tambien la fila."""
        _header, rows = self.get_plate_rows(ip, user, password,
                                            timeout=ISAPI_SYNC_TIMEOUT)
        match = None
        total = 0
        for row in rows:
            plate = self._row_plate(row)
            if not plate:
                continue
            total += 1
            if match is None and plate == plate_normalized:
                match = self._row_detail(row)
        return {"found": match is not None, "match": match, "total": total}

    def apply_plate(self, ip, user, password, action, plate_normalized,
                    on_write=None):
        """ADD/DELETE idempotente de UNA placa via read-modify-write.
        Devuelve el detail del contrato.

        `on_write(before, after)`, si viene, se llama DENTRO del lock de la
        camara tras escribir (o tras un no-op) con el allowlist de antes y el
        de despues: es lo que alimenta el historial de entradas/salidas."""
        with self._camera_lock(ip):
            return self._apply_plate_locked(ip, user, password, action,
                                            plate_normalized, on_write)

    def _apply_plate_locked(self, ip, user, password, action, plate_normalized,
                            on_write=None):
        # Export e import mueven la lista completa aunque cambie una sola
        # placa: mismos timeouts largos que el full sync.
        header, rows = self.get_plate_rows(ip, user, password,
                                           timeout=ISAPI_SYNC_TIMEOUT)
        before = self._allow_set(rows)
        present = any(self._row_plate(r) == plate_normalized for r in rows)

        if action == "ADD":
            if present:
                detail = "already_present_noop"
            else:
                rows.append(self._new_row(plate_normalized))
                self._renumber(rows)
                self.put_plate_rows(ip, user, password, header, rows,
                                    timeout=import_timeout(rows))
                detail = "created"
        elif not present:  # DELETE
            detail = "not_found_noop"
        else:
            rows = [r for r in rows if self._row_plate(r) != plate_normalized]
            self._renumber(rows)
            self.put_plate_rows(ip, user, password, header, rows,
                                timeout=import_timeout(rows))
            detail = "deleted"
        if on_write is not None:
            on_write(before, self._allow_set(rows))
        return detail

    def replace_plates(self, ip, user, password, plates_normalized,
                       on_write=None):
        """Full sync: deja la camara EXACTAMENTE con `plates_normalized`.
        `on_write` igual que en apply_plate."""
        with self._camera_lock(ip):
            return self._replace_plates_locked(ip, user, password,
                                               plates_normalized, on_write)

    def _replace_plates_locked(self, ip, user, password, plates_normalized,
                               on_write=None):
        capacity = self.plate_capacity(ip, user, password)
        if capacity is not None and len(plates_normalized) > capacity:
            raise AnprCameraError(
                "LIST_FULL",
                f"{len(plates_normalized)} placas > capacidad {capacity}")
        # Solo se reusa el header del export; las filas viejas se descartan
        # (pero su allowlist queda como el "antes" del historial).
        # Export e import escalan con el tamano de la lista (ver los timeouts
        # arriba): el export con el largo fijo, el import segun las filas.
        header, old_rows = self.get_plate_rows(ip, user, password,
                                               timeout=ISAPI_SYNC_TIMEOUT)
        rows, seen = [], set()
        for plate in plates_normalized:
            if not plate or plate in seen:
                continue
            seen.add(plate)
            rows.append(self._new_row(plate))
        self._renumber(rows)
        self.put_plate_rows(ip, user, password, header, rows,
                            timeout=import_timeout(rows))
        if on_write is not None:
            on_write(self._allow_set(old_rows), set(seen))
        return len(seen)


class AnprService:
    """Fan-out de operaciones de placas a las camaras del request.

    En la practica el backend manda UNA camara por request, pero `cameras`
    es array por contrato (simetria con terminals de /update-card).

    Con `trace` (AnprTraceStore) cada escritura queda en el historial de la
    placa: que entro, que salio, que fallo. El historial nunca puede tumbar
    una sincronizacion: si falla, se loguea y la escritura sigue su curso."""

    def __init__(self, channel=DEFAULT_CHANNEL, trace=None):
        self.client = AnprCameraClient(channel=channel)
        self.trace = trace

    @staticmethod
    def _targets(cameras):
        for camera in cameras or []:
            ip = (camera.get("ip") or "").strip()
            if not ip:
                continue
            yield ip, camera.get("user") or "admin", camera.get("password") or ""

    def _traced(self, ip, kind, source, context):
        """Devuelve (on_write, on_error, counts) para una escritura sobre `ip`.
        `counts` se llena con {added, removed, external} si se registro."""
        started = time.monotonic()
        context = context or {}
        counts = {}

        def elapsed():
            return int((time.monotonic() - started) * 1000)

        def on_write(before, after):
            if self.trace is None:
                return
            try:
                counts.update(self.trace.record_write(
                    ip, kind, source, before, after,
                    camera_id=context.get("cameraId"),
                    request_id=context.get("requestId"), duration_ms=elapsed()))
            except Exception:
                log.exception("No se pudo registrar el historial de %s en %s",
                              kind, ip)

        def on_error(code):
            if self.trace is None:
                return
            try:
                self.trace.record_write_error(
                    ip, kind, code, camera_id=context.get("cameraId"),
                    request_id=context.get("requestId"), duration_ms=elapsed())
            except Exception:
                log.exception("No se pudo registrar el fallo de %s en %s",
                              kind, ip)

        return on_write, on_error, counts

    def update_plate(self, action, plate_normalized, cameras, context=None):
        """Devuelve (results, error_code): error_code None si todo OK.
        `context` = {requestId, cameraId} del request, para el historial."""
        results, error_code = [], None
        for ip, user, password in self._targets(cameras):
            on_write, on_error, counts = self._traced(ip, action, "UPDATE", context)
            try:
                detail = self.client.apply_plate(ip, user, password,
                                                 action, plate_normalized,
                                                 on_write=on_write)
                results.append(dict({"ip": ip, "ok": True, "detail": detail}, **counts))
            except AnprCameraError as e:
                log.error("update-plate %s %s fallo en %s: %s",
                          action, plate_normalized, ip, e.code)
                on_error(e.code)
                results.append({"ip": ip, "ok": False, "detail": e.code})
                error_code = _worst(error_code, e.code)
        return results, error_code

    def sync_plates(self, plates_normalized, cameras, context=None):
        results, error_code = [], None
        for ip, user, password in self._targets(cameras):
            on_write, on_error, counts = self._traced(ip, "FULL_SYNC", "SYNC", context)
            try:
                applied = self.client.replace_plates(ip, user, password,
                                                     plates_normalized,
                                                     on_write=on_write)
                # added/removed/external: lo que cambio en la camara con este
                # envio (aditivo al contrato; Aditum hoy ignora el body).
                results.append(dict({"ip": ip, "ok": True,
                                     "detail": f"replaced_{applied}"}, **counts))
            except AnprCameraError as e:
                log.error("sync-plates (%s placas) fallo en %s: %s",
                          len(plates_normalized), ip, e.code)
                on_error(e.code)
                results.append({"ip": ip, "ok": False, "detail": e.code})
                error_code = _worst(error_code, e.code)
        return results, error_code

    def apply_test_plate(self, ip, user, password, action, plate_normalized):
        """ADD/DELETE manual desde el editor (/anpr-test). Queda en el
        historial como TEST: un cambio a mano tambien explica por que una
        placa entro o salio. Relanza AnprCameraError."""
        on_write, on_error, _counts = self._traced(ip, f"TEST_{action}", "TEST", None)
        try:
            return self.client.apply_plate(ip, user, password, action,
                                           plate_normalized, on_write=on_write)
        except AnprCameraError as e:
            on_error(e.code)
            raise


# Prioridad al reportar el fallo agregado: la falta de red (504) manda sobre
# los errores de camara (502); LIST_FULL manda sobre CAMERA_ERROR generico.
_SEVERITY = ["CAMERA_ERROR", "CAMERA_AUTH", "LIST_FULL",
             "CAMERA_TIMEOUT", "CAMERA_UNREACHABLE"]


def _worst(current, new):
    if current is None:
        return new
    return new if _SEVERITY.index(new) > _SEVERITY.index(current) else current


def http_status_for(error_code):
    """Mapeo del contrato: 504 = no se alcanzo la camara a tiempo;
    502 = la camara respondio con error."""
    if error_code in ("CAMERA_UNREACHABLE", "CAMERA_TIMEOUT"):
        return 504
    return 502
