"""Consultas de SOLO LECTURA a la camara ANPR para el diagnostico de placas.

Complementan el historial local (anpr_trace) con lo que solo sabe la camara:
  - su reloj (las lecturas traen la hora de la camara, no la del Pi);
  - su log de alarmas, que en firmwares que lo registran dice cuando se
    activo la salida de alarma (ALARM OUT) que abre la aguja;
  - que soporta ese modelo/firmware (sonda), para saber que evidencia de
    apertura se puede obtener en cada instalacion.

Ninguna funcion lanza: cada una devuelve {"supported", "ok", ...} y un
error clasificado. Las credenciales llegan en el request y no se guardan.

OJO: los formatos de logSearch y de Event/triggers varian entre firmwares.
El parseo es generico (por nombre de tag) y la sonda devuelve un extracto
crudo para poder verificarlo contra la camara real.
"""
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from .anpr import ISAPI_SYNC_TIMEOUT, AnprCameraError

log = logging.getLogger("aditum.anpr.diag")

DIAG_TIMEOUT = (3, 8)
LOG_PAGE_SIZE = 100
LOG_MAX_PAGES = 10
SNIPPET_CHARS = 600

# Minor types del log que corresponden a la salida de alarma. El texto varia
# por firmware ("Alarm Output Start", "alarmOutStart", "Alarm Out"...).
_ALARM_OUT = re.compile(r"alarm\s*out", re.IGNORECASE)
_ALARM_IN = re.compile(r"alarm\s*in", re.IGNORECASE)


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _parse(content):
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _first(root, *names):
    if root is None:
        return None
    wanted = {n.lower() for n in names}
    for e in root.iter():
        if _local(e.tag).lower() in wanted and e.text and e.text.strip():
            return e.text.strip()
    return None


def _snippet(resp):
    try:
        text = resp.text or ""
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text)[:SNIPPET_CHARS]


def _error(code, message=""):
    return {"supported": None, "ok": False, "error": code, "message": message}


def _camera_dt(value):
    """Hora del log/reloj de la camara -> datetime con zona. Hikvision suele
    mandar la hora LOCAL de la camara con una 'Z' que no significa UTC; sin
    zona confiable se interpreta como hora local del Pi (mismo condominio)."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def camera_time(client, ip, user, password):
    """Reloj de la camara vs el del Pi. skewSeconds > 0: camara adelantada."""
    try:
        resp = client.isapi("GET", ip, user, password, "/ISAPI/System/time",
                            timeout=DIAG_TIMEOUT)
    except AnprCameraError as e:
        return _error(e.code, e.message)
    if resp.status_code == 401:
        return _error("CAMERA_AUTH")
    if resp.status_code != 200:
        return {"supported": False, "ok": False, "error": f"http_{resp.status_code}"}
    root = _parse(resp.content)
    local_time = _first(root, "localTime")
    camera_dt = None
    if local_time:
        try:
            camera_dt = datetime.fromisoformat(local_time.replace("Z", "+00:00"))
            if camera_dt.tzinfo is None:
                camera_dt = camera_dt.astimezone()
        except ValueError:
            camera_dt = None
    pi_now = datetime.now().astimezone()
    skew = None if camera_dt is None else round((camera_dt - pi_now).total_seconds())
    return {"supported": True, "ok": camera_dt is not None,
            "cameraTime": local_time, "piTime": pi_now.isoformat(timespec="seconds"),
            "timeMode": _first(root, "timeMode"), "timeZone": _first(root, "timeZone"),
            "skewSeconds": skew}


def _log_search_body(start, end, position, meta_id):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<CMSearchDescription>"
        f"<searchID>{uuid.uuid4()}</searchID>"
        f"<metaId>{meta_id}</metaId>"
        "<timeSpanList><timeSpan>"
        f"<startTime>{start.strftime(fmt)}</startTime>"
        f"<endTime>{end.strftime(fmt)}</endTime>"
        "</timeSpan></timeSpanList>"
        f"<maxResults>{LOG_PAGE_SIZE}</maxResults>"
        f"<searchResultPostion>{position}</searchResultPostion>"
        "</CMSearchDescription>").encode("utf-8")


def _log_entries(root):
    entries = []
    for item in root.iter():
        if _local(item.tag) != "searchMatchItem":
            continue
        fields = {}
        for e in item.iter():
            if e is not item and e.text and e.text.strip() and len(e) == 0:
                fields.setdefault(_local(e.tag), e.text.strip())
        kind = " ".join(str(fields.get(k, "")) for k in
                        ("metaId", "majorType", "minorType", "logType")).strip()
        when = (fields.get("StartDateTime") or fields.get("startTime")
                or fields.get("time") or fields.get("dateTime"))
        dt = _camera_dt(when)
        entries.append({
            "time": when,
            "ts": dt.timestamp() if dt else None,
            "type": kind,
            "alarmOut": bool(_ALARM_OUT.search(kind)),
            "alarmIn": bool(_ALARM_IN.search(kind)),
            "port": fields.get("paraType") or fields.get("alarmOutPort")
                    or fields.get("outputIOPortID"),
        })
    return entries


# subStatusCode con que la camara rechaza la busqueda en su log cuando no
# tiene SD ni NAS (verificado en iDS-2CD7A46G0/P-IZHSY, firmware V5.8.61): el
# log de alarmas solo existe si hay almacenamiento. No se reintenta.
NO_STORAGE_SUBSTATUS = "invalidMemoryCardOrNAS"


def _status_reason(resp):
    """subStatusCode (o statusString) del ResponseStatus de error de la camara."""
    root = _parse(resp.content)
    return _first(root, "subStatusCode") or _first(root, "statusString")


def search_logs(client, ip, user, password, start, end, meta_id="log.std-cgi.com/Alarm"):
    """Log de alarmas de la camara entre `start` y `end` (datetimes locales).
    Pagina hasta LOG_MAX_PAGES. Devuelve todas las entradas parseadas; las de
    salida de alarma vienen con alarmOut=True (minorType "alarmOut").

    Sin SD/NAS la camara responde 403 invalidMemoryCardOrNAS: se devuelve
    error NO_STORAGE (no soportado en esta instalacion), sin reintentar."""
    entries, position = [], 0
    for _page in range(LOG_MAX_PAGES):
        try:
            resp = client.isapi("POST", ip, user, password,
                                "/ISAPI/ContentMgmt/logSearch",
                                timeout=ISAPI_SYNC_TIMEOUT,
                                data=_log_search_body(start, end, position, meta_id),
                                headers={"Content-Type": "application/xml"})
        except AnprCameraError as e:
            return dict(_error(e.code, e.message), entries=entries)
        if resp.status_code == 401:
            return dict(_error("CAMERA_AUTH"), entries=entries)
        if resp.status_code != 200:
            reason = _status_reason(resp)
            no_storage = reason == NO_STORAGE_SUBSTATUS
            return {"supported": False, "ok": False,
                    "error": "NO_STORAGE" if no_storage else f"http_{resp.status_code}",
                    "reason": reason, "entries": entries, "snippet": _snippet(resp)}
        root = _parse(resp.content)
        if root is None:
            return {"supported": False, "ok": False, "error": "respuesta no XML",
                    "entries": entries, "snippet": _snippet(resp)}
        page = _log_entries(root)
        entries += page
        status = (_first(root, "responseStatusStrg") or "").upper()
        if status != "MORE" or not page:
            break
        position += len(page)
    return {"supported": True, "ok": True, "entries": entries,
            "truncated": len(entries) >= LOG_PAGE_SIZE * LOG_MAX_PAGES}


def _all(root, name):
    if root is None:
        return []
    return [e.text.strip() for e in root.iter()
            if _local(e.tag) == name and e.text and e.text.strip()]


def _count(root, name):
    return 0 if root is None else sum(1 for e in root.iter() if _local(e.tag) == name)


def _http_hosts(root):
    """A donde postea la camara sus eventos (HTTP Listening)."""
    hosts = []
    if root is None:
        return hosts
    for e in root.iter():
        if _local(e.tag) != "HttpHostNotification":
            continue
        hosts.append({"url": _first(e, "url"), "ip": _first(e, "ipAddress"),
                      "port": _first(e, "portNo"), "protocol": _first(e, "protocolType")})
    return hosts


# Endpoints que la sonda prueba: (nombre, path, extractor de campos). Todos
# GET. Verificado contra iDS-2CD7A46G0/P-IZHSY V5.8.61: existen todos salvo
# Parking/barrierGate (404, solo modelos de parqueo).
_PROBE_GETS = (
    ("deviceInfo", "/ISAPI/System/deviceInfo",
     lambda r: {f: _first(r, f) for f in ("deviceName", "model", "firmwareVersion",
                                          "firmwareReleasedDate")}),
    ("time", "/ISAPI/System/time",
     lambda r: {f: _first(r, f) for f in ("localTime", "timeMode", "timeZone")}),
    ("ntpServers", "/ISAPI/System/time/ntpServers",
     lambda r: {"hostName": _first(r, "hostName"), "ipAddress": _first(r, "ipAddress"),
                "synchronizeInterval": _first(r, "synchronizeInterval")}),
    ("ioCapabilities", "/ISAPI/System/IO/capabilities",
     lambda r: {"inputs": _first(r, "IOInputPortNums"), "outputs": _first(r, "IOOutputPortNums")}),
    ("ioOutputs", "/ISAPI/System/IO/outputs",
     lambda r: {"count": _count(r, "IOOutputPort"), "ids": _all(r, "id"),
                "outputState": _all(r, "outputState"),
                "pulseDurationMs": _all(r, "pulseDuration")}),
    ("ioOutput1Status", "/ISAPI/System/IO/outputs/1/status",
     lambda r: {"ioState": _first(r, "ioState", "IOState")}),
    ("ioInputs", "/ISAPI/System/IO/inputs",
     lambda r: {"count": _count(r, "IOInputPort"), "enabled": _all(r, "enabled"),
                "triggering": _all(r, "triggering")}),
    # El linkage allowlist -> salida de alarma de las camaras ANPR NO siempre
    # aparece aca: en V5.8.61 este trigger solo trae "center" aunque la camara
    # abra la aguja con su allowlist (se configura en otra parte del firmware).
    ("vehicleDetectionTrigger", "/ISAPI/Event/triggers/vehicledetection-1",
     lambda r: {"notificationMethod": _all(r, "notificationMethod"),
                "outputIOPortID": _all(r, "outputIOPortID") + _all(r, "dynOutputIOPortID")}),
    ("httpHosts", "/ISAPI/Event/notification/httpHosts",
     lambda r: {"hosts": _http_hosts(r)}),
    ("storage", "/ISAPI/ContentMgmt/Storage",
     lambda r: {"hdd": _count(r, "hdd"), "nas": _count(r, "nas"),
                "workMode": _first(r, "workMode")}),
    ("logSearchCapabilities", "/ISAPI/ContentMgmt/logSearch/capabilities",
     lambda r: {"alarmMinorTypes": next((e.get("opt") for e in r.iter()
                                         if _local(e.tag) == "minorType" and e.get("opt")
                                         and "alarm" in e.get("opt", "").lower()), None)
                if r is not None else None}),
    ("trafficCapabilities", "/ISAPI/Traffic/capabilities",
     lambda r: {"plateListNum": _first(r, "plateListNum")}),
    ("barrierGate", "/ISAPI/Parking/channels/1/barrierGate",
     lambda r: {"status": _first(r, "barrierGateStatus", "status")}),
)


def _supported(status):
    """200 = lo tiene; 404 ("Can't find process for service") = el firmware no
    lo tiene; 403 ("Invalid Operation") = existe pero no responde a esta
    consulta. Otro codigo: indeterminado."""
    return {200: True, 404: False}.get(status)


def probe(client, ip, user, password):
    """Que expone ESTA camara para confirmar aperturas. Solo lectura."""
    report = {}
    for name, path, extract in _PROBE_GETS:
        try:
            resp = client.isapi("GET", ip, user, password, path, timeout=DIAG_TIMEOUT)
        except AnprCameraError as e:
            report[name] = {"path": path, "ok": False, "supported": None, "error": e.code}
            if e.code == "CAMERA_UNREACHABLE":
                break
            continue
        entry = {"path": path, "status": resp.status_code, "ok": resp.status_code == 200,
                 "supported": _supported(resp.status_code)}
        if resp.status_code == 200:
            root = _parse(resp.content)
            try:
                entry["fields"] = extract(root)
            except Exception:
                entry["fields"] = {}
            entry["snippet"] = _snippet(resp)
        else:
            entry["reason"] = _status_reason(resp)
        report[name] = entry
    now = datetime.now().replace(microsecond=0)
    logs = search_logs(client, ip, user, password, now - timedelta(days=1), now,
                       meta_id="log.std-cgi.com")
    types = {}
    for entry in logs.get("entries", []):
        types[entry["type"]] = types.get(entry["type"], 0) + 1
    report["logSearch24h"] = {
        "ok": logs.get("ok"), "supported": logs.get("supported"),
        "error": logs.get("error"), "reason": logs.get("reason"),
        "entries": len(logs.get("entries", [])), "types": types,
        "alarmOutEntries": sum(1 for e in logs.get("entries", []) if e["alarmOut"]),
        "snippet": logs.get("snippet"),
    }
    report["summary"] = _probe_summary(report)
    return report


def _probe_summary(report):
    """Conclusiones legibles: que evidencia de apertura da esta camara."""
    def fields(name):
        return report.get(name, {}).get("fields") or {}
    outputs = fields("ioOutputs")
    inputs = fields("ioInputs")
    storage = fields("storage")
    log24 = report.get("logSearch24h", {})
    trigger_outputs = fields("vehicleDetectionTrigger").get("outputIOPortID") or []
    alarm_types = (fields("logSearchCapabilities").get("alarmMinorTypes") or "")
    has_storage = bool(storage.get("hdd") or storage.get("nas"))
    log_ok = bool(log24.get("supported"))
    return {
        "model": fields("deviceInfo").get("model"),
        "firmware": fields("deviceInfo").get("firmwareVersion"),
        "alarmOutputs": outputs.get("count") or 0,
        "alarmOutputPulseMs": outputs.get("pulseDurationMs") or [],
        "alarmInputs": inputs.get("count") or 0,
        # True solo si el trigger de deteccion lo muestra. None = no visible
        # por ISAPI (varios firmwares lo guardan en otro lado): NO significa
        # que la camara no abra.
        "linkageToAlarmOut": True if trigger_outputs else None,
        "postsTo": fields("httpHosts").get("hosts") or [],
        "hasStorage": has_storage,
        "logSearchSupported": log_ok,
        "logSearchError": log24.get("error"),
        "logKnowsAlarmOut": "alarmout" in alarm_types.lower(),
        "logsAlarmOut": bool(log24.get("alarmOutEntries")),
        # Se puede confirmar la orden de apertura (ALARM OUT) por el log de la
        # camara solo con SD/NAS: sin almacenamiento el log no se puede buscar.
        "canConfirmOpenOrder": log_ok and "alarmout" in alarm_types.lower(),
        # La apertura fisica necesita un sensor en una ALARM IN libre.
        "canConfirmPhysicalOpen": bool(inputs.get("count")),
        "barrierGateApi": bool(report.get("barrierGate", {}).get("ok")),
    }
