"""Diagnostico del historial de UNA placa (POST /anpr-plate-history).

Arma, a partir del historial local (anpr_trace) y opcionalmente de la camara
en vivo (anpr_camera_diag), la respuesta a "que paso con esta placa":
  - cuando entro y cuando salio de la lista de cada camara;
  - cada lectura: en que lista la reporto la camara y si en ese momento
    estaba en el allowlist segun el historial;
  - si hay evidencia de que se abrio (log de ALARM OUT de la camara o un
    evento IO de un sensor);
  - lecturas de placas parecidas (mala lectura del OCR);
  - `findings`: la explicacion automatica, por codigo.

Tres cosas distintas que la respuesta NO mezcla:
  leida en allowlist  -> la camara deberia abrir (si su linkage esta activo)
  orden de apertura   -> entrada ALARM OUT en el log de la camara
  apertura fisica     -> evento IO de un sensor cableado a la camara
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime

from . import anpr_camera_diag
from .anpr import AnprCameraError, camera_host
from .anpr_events import ALLOW_LIST_NAMES
from .anpr_trace import (ADDED, REMOVED, SOURCE_BASELINE, SOURCE_EXTERNAL, SOURCE_SYNC,
                         SOURCE_TEST, SOURCE_UPDATE, ts_to_iso)

log = logging.getLogger("aditum.anpr.history")

SYNC_ERROR_LOOKBACK = 24 * 3600   # fallos de sync que "explican" una lectura
# Ventanas para cruzar una lectura con la evidencia de apertura. NO validadas
# contra hardware: la camara de prueba (iDS-2CD7A46G0, V5.8.61) no tiene SD,
# asi que su log de alarmas no se puede consultar, y no tiene sensor en ALARM IN.
IO_WINDOW = (-5, 30)              # segundos alrededor de la lectura (reloj Pi)
ALARM_OUT_WINDOW = 15             # +- segundos (reloj de la camara)
LOG_MARGIN = 60
SKEW_WARN_SECONDS = 120
LIVE_BUDGET_SECONDS = 45          # nginx corta a los 60 s
MAX_OCCURRENCES = 20
MAX_SIMILAR_PLATES = 20

# Origen de una entrada/salida, en palabras (para los mensajes de findings).
SOURCE_TEXT = {
    SOURCE_SYNC: "envío completo de Aditum",
    SOURCE_UPDATE: "alta/baja de Aditum",
    SOURCE_TEST: "prueba desde el editor",
    SOURCE_BASELINE: "lista que ya tenía la cámara",
    SOURCE_EXTERNAL: "cambio hecho por fuera del Pi",
}


def _human(at, seconds=False):
    """'2026-10-29T10:12:00-06:00' -> '2026-10-29 10:12' (hora local de la lectura)."""
    return (at or "")[:19 if seconds else 16].replace("T", " ")


def _via(change):
    return "%s, %s" % (_human(change["at"]), SOURCE_TEXT.get(change["source"], change["source"]))

# Confusiones tipicas del OCR: se comparan las placas "canonizadas".
_CONFUSABLE = str.maketrans({"0": "O", "Q": "O", "D": "O", "1": "I", "L": "I",
                             "8": "B", "5": "S", "2": "Z", "6": "G", "7": "T"})


def list_label(vehicle_list):
    value = (vehicle_list or "").lower()
    if value in ALLOW_LIST_NAMES:
        return "allowlist"
    if value in ("blacklist", "blocklist"):
        return "blocklist"
    if value == "otherlist":
        return "otherlist"
    return value or "sin_dato"


def _one_edit_apart(a, b):
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def is_similar(a, b):
    return a != b and (a.translate(_CONFUSABLE) == b.translate(_CONFUSABLE)
                       or _one_edit_apart(a, b))


class _Findings:
    """Agrupa los hallazgos por codigo para no repetir 50 veces lo mismo."""

    def __init__(self):
        self._by_code = {}

    def add(self, code, severity, message, at=None, camera_ip=None):
        item = self._by_code.get(code)
        if item is None:
            item = self._by_code[code] = {
                "code": code, "severity": severity, "count": 0,
                "message": message, "occurrences": []}
        item["count"] += 1
        item["message"] = message  # el mas reciente describe el caso
        if at and len(item["occurrences"]) < MAX_OCCURRENCES:
            item["occurrences"].append({"at": at, "camera": camera_ip})

    def items(self):
        order = {"high": 0, "medium": 1, "info": 2}
        return sorted(self._by_code.values(), key=lambda f: order.get(f["severity"], 3))


def _change_view(change):
    if not change:
        return None
    return {"at": change["at"], "camera": change["camera_ip"],
            "change": change["change"], "source": change["source"],
            "requestId": change["request_id"], "cameraId": change["camera_id"]}


def _photos(read, captures):
    names = [n for n in (read.get("photos") or "").split(",") if n]
    if captures is not None:
        names = [n for n in names if captures.file_path(n) is not None]
    return [f"/anpr-captures/{n}" for n in names]


# ----------------------------------------------------------------------
# Consulta en vivo a la camara (opcional, con credenciales del request)
# ----------------------------------------------------------------------
def _log_coverage(logs, start, end):
    """Tramo [desde, hasta] del que se puede afirmar "no hubo ALARM OUT". Si
    la camara tenia mas entradas de las que se traen (truncated), solo vale
    el tramo que ocupan las entradas recibidas; fuera de el no se sabe."""
    if not logs.get("ok"):
        return None
    if not logs.get("truncated"):
        return [start.timestamp(), end.timestamp()]
    stamps = [e["ts"] for e in logs.get("entries", []) if e["ts"] is not None]
    return [min(stamps), max(stamps)] if stamps else None


def _live_for_camera(client, cred, plate, reads, deadline=None):
    ip, user, password = cred["ip"], cred.get("user") or "admin", cred.get("password") or ""
    host = camera_host(ip)   # las lecturas se guardan por host, sin puerto
    out = {}
    try:
        out["find"] = dict(client.find_plate(ip, user, password, plate), ok=True)
    except AnprCameraError as e:
        out["find"] = {"ok": False, "error": e.code}
    out["time"] = anpr_camera_diag.camera_time(client, ip, user, password)
    stamps = [r["captured_ts"] or r["received_ts"] for r in reads if r["camera_ip"] == host]
    if stamps:
        start = datetime.fromtimestamp(min(stamps) - LOG_MARGIN)
        end = datetime.fromtimestamp(max(stamps) + LOG_MARGIN)
        logs = anpr_camera_diag.search_logs(client, ip, user, password, start, end,
                                            deadline=deadline)
        logs["covered"] = _log_coverage(logs, start, end)
        out["alarmLog"] = logs
    return out


def collect_live(client, credentials, plate, reads):
    """{host: resultado} consultando cada camara en paralelo, con presupuesto.
    Pasado el presupuesto se responde con lo que haya; el hilo que quedo
    corriendo deja de paginar el log al ver el deadline."""
    if not client or not credentials:
        return {}
    deadline = time.monotonic() + LIVE_BUDGET_SECONDS
    pool = ThreadPoolExecutor(max_workers=max(1, len(credentials)))
    futures = {pool.submit(_live_for_camera, client, c, plate, reads, deadline):
               camera_host(c["ip"]) for c in credentials}
    done, _pending = wait(futures, timeout=LIVE_BUDGET_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)
    live = {}
    for future, ip in futures.items():
        if future in done and future.exception() is None:
            live[ip] = future.result()
        elif future in done:
            log.error("Diagnostico en vivo de %s fallo", ip,
                      exc_info=future.exception())
            live[ip] = {"error": "INTERNAL_ERROR"}
        else:
            live[ip] = {"error": "TIMEOUT_BUDGET", "partial": True}
    return live


# ----------------------------------------------------------------------
# Evidencia de apertura de una lectura
# ----------------------------------------------------------------------
def _open_evidence(read, io_events, live):
    ip = read["camera_ip"]
    for io in io_events:
        if io["camera_ip"] != ip:
            continue
        delta = io["received_ts"] - read["received_ts"]
        if IO_WINDOW[0] <= delta <= IO_WINDOW[1] and io.get("state") != "inactive":
            return True, "PHYSICAL", (f"evento {io['event_type']} puerto {io['port']} "
                                      f"a las {_human(io['received_at'], True)}")
    alarm_log = (live.get(ip) or {}).get("alarmLog") or {}
    camera_ts = read["captured_ts"] or read["received_ts"]
    if alarm_log.get("ok"):
        for entry in alarm_log.get("entries", []):
            if entry["alarmOut"] and entry["ts"] is not None \
                    and abs(entry["ts"] - camera_ts) <= ALARM_OUT_WINDOW:
                return True, "OPEN_ORDER", f"log de la cámara: {entry['type']} a las {_human(entry['time'], True)}"
        covered = alarm_log.get("covered")
        if covered and covered[0] <= camera_ts <= covered[1]:
            return False, "OPEN_ORDER", ("el log de la cámara no registra ALARM OUT "
                                         f"±{ALARM_OUT_WINDOW} s de la lectura")
        if alarm_log.get("truncated"):
            return "unknown", None, ("el log de la cámara tiene más entradas de las que "
                                     "se consultan y no cubre esta lectura: acorte el rango")
    if alarm_log.get("error") == "NO_STORAGE":
        return "unknown", None, ("la cámara no tiene SD: su registro de alarmas no se "
                                 "puede consultar, y no hay sensor de apertura")
    return "unknown", None, "sin sensor de apertura ni log de alarmas de la cámara"


# ----------------------------------------------------------------------
# Armado de la respuesta
# ----------------------------------------------------------------------
def plate_history(trace, plate, camera_ips, since_ts, until_ts,
                  client=None, credentials=None, captures=None):
    changes = trace.plate_changes(plate, camera_ips, until_ts=until_ts)
    reads = trace.reads(plate, camera_ips, since_ts, until_ts)
    sync_errors = trace.sync_log(camera_ips, since_ts - SYNC_ERROR_LOOKBACK,
                                 until_ts, only_errors=True)
    io_events = trace.io_events(camera_ips, since_ts - 60, until_ts + 60)
    live = collect_live(client, credentials, plate, reads)
    findings = _Findings()

    ips = sorted(set(camera_ips or []) | {c["camera_ip"] for c in changes}
                 | {r["camera_ip"] for r in reads if r["camera_ip"]} | set(live))
    known = {c["camera_ip"]: c for c in trace.cameras()}

    read_views, timeline = [], []
    for change in changes:
        timeline.append({"ts": change["ts"], "at": change["at"], "stage": "LIST",
                         "camera": change["camera_ip"],
                         "event": "ENTRO" if change["change"] == ADDED else "SALIO",
                         "source": change["source"], "requestId": change["request_id"]})
        if change["source"] == SOURCE_EXTERNAL and change["ts"] >= since_ts:
            findings.add("CHANGED_OUTSIDE_ADITUM", "medium",
                         "La lista de la cámara tenía esta placa distinta a lo que el "
                         "Pi había escrito: alguien la tocó por fuera de Aditum o la "
                         "cámara se reseteó (detectado al escribir).",
                         change["at"], change["camera_ip"])
    for error in sync_errors:
        if error["ts"] >= since_ts:
            timeline.append({"ts": error["ts"], "at": error["at"], "stage": "SYNC_ERROR",
                             "camera": error["camera_ip"], "event": error["kind"],
                             "error": error["error_code"], "requestId": error["request_id"]})

    counters = {"allowlist": 0, "otherlist": 0, "notOnList": 0,
                "otherWhileOnAllowlist": 0, "noOpenEvidence": 0, "notOpened": 0}
    for read in reads:
        view = _read_view(read, trace, plate, changes, sync_errors, io_events,
                          live, captures, findings, counters)
        read_views.append(view)
        timeline.append({"ts": read["received_ts"], "at": read["received_at"],
                         "stage": "READ", "camera": read["camera_ip"],
                         "plateRead": view["plateRead"], "confidence": view["confidence"],
                         "list": view["list"], "onAllowlist": view["onAllowlistAtThatTime"],
                         "onAllowlistAtCapture": view["onAllowlistAtCaptureTime"],
                         "expectedOpen": view["expectedOpen"], "opened": view["opened"],
                         "openStage": view["openStage"], "openEvidence": view["openEvidence"],
                         "photos": view["photos"]})
        if view["openStage"]:
            timeline.append({"ts": read["received_ts"] + 0.001, "at": read["received_at"],
                             "stage": view["openStage"], "camera": read["camera_ip"],
                             "opened": view["opened"], "evidence": view["openEvidence"]})

    similar = _similar_reads(trace, plate, camera_ips, since_ts, until_ts, captures)
    if similar:
        findings.add("POSSIBLE_MISREAD", "medium",
                     "Hay lecturas de placas parecidas en el rango (%s): la cámara pudo "
                     "leer mal algún carácter." % ", ".join(s["plate"] for s in similar[:5]))
    if not reads:
        findings.add("NO_READS", "info",
                     "La cámara no envió ninguna lectura de esta placa en el rango.")

    cameras = [_camera_view(ip, trace, plate, changes, known.get(ip), live.get(ip),
                            findings) for ip in ips]
    added = [c for c in changes if c["change"] == ADDED]
    removed = [c for c in changes if c["change"] == REMOVED]
    timeline.sort(key=lambda t: t["ts"])
    for entry in timeline:
        entry.pop("ts", None)
    return {
        "plate": plate,
        "range": {"from": ts_to_iso(since_ts), "to": ts_to_iso(until_ts)},
        "generatedAt": ts_to_iso(time.time()),
        "retentionDays": trace.retention_days,
        "summary": {
            "lastAdded": _change_view(added[-1]) if added else None,
            "lastRemoved": _change_view(removed[-1]) if removed else None,
            "timesAdded": len(added), "timesRemoved": len(removed),
            "lastRead": read_views[-1]["receivedAt"] if read_views else None,
            "reads": len(read_views),
            "readsAllowlist": counters["allowlist"],
            "readsOtherList": counters["otherlist"],
            "otherListWhileOnAllowlist": counters["otherWhileOnAllowlist"],
            "readsNotOnList": counters["notOnList"],
            "readsNotOpened": counters["notOpened"],
            "readsWithoutOpenEvidence": counters["noOpenEvidence"],
        },
        "cameras": cameras,
        "listHistory": [_change_view(c) for c in changes],
        "syncErrors": [{"at": e["at"], "camera": e["camera_ip"], "kind": e["kind"],
                        "error": e["error_code"], "requestId": e["request_id"]}
                       for e in sync_errors],
        "reads": read_views,
        "similarReads": similar,
        "findings": findings.items(),
        "timeline": timeline,
    }


def _read_view(read, trace, plate, changes, sync_errors, io_events, live,
               captures, findings, counters):
    ip = read["camera_ip"]
    state, change = trace.membership_at(ip, plate, read["received_ts"])
    label = list_label(read["vehicle_list"])
    opened, open_stage, evidence = _open_evidence(read, io_events, live)
    if label != "allowlist" and opened is not True:
        # Rechazada y sin apertura: es lo esperado, no va como evento aparte.
        open_stage = None
    skew = None
    if read["captured_ts"] is not None:
        skew = round(read["received_ts"] - read["captured_ts"])
    at = read["received_at"]

    if label == "allowlist":
        counters["allowlist"] += 1
        if state is False:
            findings.add("ALLOWLIST_READ_NOT_IN_HISTORY", "medium",
                         "La cámara la autorizó pero, según el historial del Pi, no "
                         "estaba en su lista (cambio por fuera o historial incompleto).",
                         at, ip)
        if opened is False:
            counters["notOpened"] += 1
            findings.add("READ_ALLOWLIST_NOT_OPENED", "high",
                         "Se leyó en allowlist pero el log de la cámara no registra la "
                         "salida de alarma (ALARM OUT) que abre la aguja.", at, ip)
        elif opened == "unknown":
            counters["noOpenEvidence"] += 1
    elif label in ("otherlist", "blocklist"):
        counters["otherlist"] += 1
        _explain_rejection(read, state, change, changes, sync_errors, findings, counters)
    at_capture = None
    if skew is not None and abs(skew) > SKEW_WARN_SECONDS:
        # O el reloj de la camara esta corrido, o la camara reenvio la lectura
        # tarde (ANR, con el Pi caido). En el segundo caso lo que vale es la
        # hora de captura: se da tambien la membresia en ese momento.
        captured_state, _ = trace.membership_at(ip, plate, read["captured_ts"])
        at_capture = "unknown" if captured_state is None else captured_state
        findings.add("CLOCK_SKEW", "medium",
                     f"La hora de la cámara y la del Pi difieren {skew} s en esta "
                     "lectura (reloj de la cámara corrido o lectura reenviada tarde "
                     "por ANR). Ver onAllowlistAtCaptureTime.", at, ip)
    return {
        "receivedAt": at, "capturedAt": read["captured_at"], "camera": ip,
        "cameraName": read["camera_name"], "plateRead": read["plate_raw"],
        "list": label, "outcome": read["outcome"], "confidence": read["confidence"],
        "direction": read["direction"],
        "onAllowlistAtThatTime": "unknown" if state is None else state,
        "onAllowlistAtCaptureTime": at_capture,
        "listChangeBefore": _change_view(change),
        "expectedOpen": label == "allowlist",
        "opened": opened, "openStage": open_stage, "openEvidence": evidence,
        "clockSkewSeconds": skew,
        "photos": _photos(read, captures),
    }


def _explain_rejection(read, state, change, changes, sync_errors, findings, counters):
    ip, ts, at = read["camera_ip"], read["received_ts"], read["received_at"]
    if state is None:
        findings.add("NO_HISTORY", "info",
                     "No hay historial de la lista de esta cámara para esa fecha "
                     "(anterior al despliegue o a la primera sincronización).", at, ip)
        return
    if state is True:
        counters["otherWhileOnAllowlist"] += 1
        findings.add("OTHERLIST_WHILE_ON_ALLOWLIST", "high",
                     "La cámara la leyó como %s pero estaba en su allowlist desde el %s (%s). "
                     "El problema está en la cámara: vigencia de la fila, reloj o el "
                     "match de su firmware." % (
                         read["vehicle_list"], _human(change["at"]),
                         SOURCE_TEXT.get(change["source"], change["source"])), at, ip)
        return
    counters["notOnList"] += 1
    later = next((c for c in changes if c["camera_ip"] == ip and c["ts"] > ts
                  and c["change"] == ADDED), None)
    if change and change["change"] == REMOVED:
        reason = "la habían sacado el %s" % _via(change)
    else:
        reason = "todavía no había entrado"
    if later:
        verb = "volvió a entrar" if change else "entró después"
        reason += "; %s el %s" % (verb, _via(later))
    findings.add("NOT_ON_LIST_AT_READ", "high",
                 "No estaba en la lista de la cámara al leerla: %s." % reason, at, ip)
    failed = [e for e in sync_errors if e["camera_ip"] == ip
              and ts - SYNC_ERROR_LOOKBACK <= e["ts"] <= ts]
    if failed:
        findings.add("SYNC_FAILED_BEFORE_READ", "high",
                     "En las 24 h previas fallaron %s escritura(s) en esta cámara "
                     "(último error %s el %s): la placa pudo no llegar." % (
                         len(failed), failed[-1]["error_code"], _human(failed[-1]["at"])), at, ip)


def _camera_view(ip, trace, plate, changes, known, live, findings):
    state, change = trace.membership_at(ip, plate, time.time())
    own = [c for c in changes if c["camera_ip"] == ip]
    view = {
        "ip": ip,
        "cameraId": known["camera_id"] if known else None,
        "historySince": known["first_seen_at"] if known else None,
        "lastWrite": known["last_write_at"] if known else None,
        "onListNow": "unknown" if state is None else state,
        "lastAdded": next((_change_view(c) for c in reversed(own) if c["change"] == ADDED), None),
        "lastRemoved": next((_change_view(c) for c in reversed(own) if c["change"] == REMOVED), None),
    }
    if live:
        view["live"] = {k: v for k, v in live.items() if k != "alarmLog"}
        alarm_log = live.get("alarmLog")
        if alarm_log:
            view["live"]["alarmLog"] = {
                k: alarm_log.get(k) for k in ("supported", "ok", "error", "truncated")}
            view["live"]["alarmLog"]["alarmOutEntries"] = sum(
                1 for e in alarm_log.get("entries", []) if e["alarmOut"])
        _live_findings(ip, view, live, findings)
    return view


def _live_findings(ip, view, live, findings):
    find = live.get("find") or {}
    if find.get("ok"):
        match = find.get("match") or {}
        on_camera = bool(find.get("found")) and match.get("group") == "allowlist"
        if view["onListNow"] != "unknown" and on_camera != view["onListNow"]:
            findings.add("CAMERA_LIST_DIFFERS", "high",
                         "Hoy la cámara %s la tiene en su allowlist y el historial del Pi "
                         "dice lo contrario." % ("SÍ" if on_camera else "NO"), None, ip)
        if find.get("found") and match.get("group") != "allowlist":
            findings.add("PLATE_IN_BLOCKLIST", "high",
                         "La placa está en la cámara pero en la lista %s." % match.get("group"),
                         None, ip)
        valid_to = match.get("validTo") or ""
        if find.get("found") and valid_to and valid_to < datetime.now().strftime("%Y-%m-%d"):
            findings.add("PLATE_EXPIRED_ON_CAMERA", "high",
                         f"La fila de la placa en la cámara venció el {valid_to}.", None, ip)
    skew = (live.get("time") or {}).get("skewSeconds")
    if skew is not None and abs(skew) > 60:
        findings.add("CAMERA_CLOCK_SKEW", "medium",
                     f"El reloj de la cámara está corrido {skew} s respecto del Pi.", None, ip)


def _similar_reads(trace, plate, camera_ips, since_ts, until_ts, captures):
    """Placas parecidas leidas en el rango. La similitud se evalua sobre las
    placas distintas (agrupadas en SQL), y solo para las que califican se
    trae su ultima lectura (camara y fotos)."""
    grouped = {}
    for row in trace.similar_candidates(plate, camera_ips, since_ts, until_ts):
        if not is_similar(plate, row["plate"]):
            continue
        item = grouped.setdefault(row["plate"], {
            "plate": row["plate"], "count": 0, "lists": set(), "lastAt": None})
        item["count"] += row["count"]
        item["lists"].add(list_label(row["vehicle_list"]))
        item["lastAt"] = max(item["lastAt"] or "", row["last_at"] or "") or None
    items = sorted(grouped.values(), key=lambda i: -i["count"])[:MAX_SIMILAR_PLATES]
    for item in items:
        item["lists"] = sorted(item["lists"])
        last = trace.latest_read(item["plate"], camera_ips, since_ts, until_ts)
        item["camera"] = last["camera_ip"] if last else None
        item["photos"] = _photos(last, captures) if last else []
    return items
