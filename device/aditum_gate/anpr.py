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
  - GET/PUT /ISAPI/Traffic/channels/<ch>/licensePlateAuditData?fileType=xml
      exporta/importa la lista completa (read-modify-write para individual,
      replace para full sync). El import puede responder errorCode
      "overLimit" = lista llena, que se reporta explicito (LIST_FULL).
  - POST /ISAPI/Traffic/channels/<ch>/searchLPListAudit
      consulta paginada de la lista (verificacion).
  - GET /ISAPI/Traffic/capabilities → plateListNum (capacidad maxima).

NOTA timeouts: el backend corta la conexion a los 5 s (deviceRestTemplate).
Por eso aqui NO se usa la sesion compartida de httpclient (trae reintentos
automaticos que multiplican la latencia): cada llamada ISAPI va directa con
timeouts cortos y un solo intento — si la camara no responde, se devuelve
504 rapido y el backend reintenta en su proximo ciclo.
"""
import copy
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from requests.auth import HTTPDigestAuth

log = logging.getLogger("aditum.anpr")

# (connect, read) por llamada ISAPI. Un update-plate hace GET+PUT: peor caso
# ~2.5s+2.5s, dentro del budget de 5 s del backend en LAN sana.
ISAPI_TIMEOUT = (2, 2.5)
# El PUT del full sync sube la lista completa; un poco mas de aire de lectura.
ISAPI_SYNC_TIMEOUT = (2, 4)

DEFAULT_CHANNEL = 1

# Normalizacion espejo de LicensePlateUtil (aditum-jh): mayusculas + solo
# [A-Z0-9]. Se usa SOLO para comparar contra entradas legadas cargadas a
# mano en la camara; el valor que manda el backend ya viene normalizado y
# NO se re-transforma.
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def _normalize(plate):
    if plate is None:
        return None
    cleaned = _NON_ALNUM.sub("", plate).upper()
    return cleaned or None


def _local(tag):
    """Nombre local de un tag XML, ignorando namespace."""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_all(root, name):
    return [e for e in root.iter() if _local(e.tag) == name]


def _find_child_text(element, name):
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _set_child_text(element, name, value):
    for child in element:
        if _local(child.tag) == name:
            child.text = value
            return True
    return False


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

    def _list_url(self, ip):
        return (f"http://{ip}/ISAPI/Traffic/channels/{self.channel}"
                f"/licensePlateAuditData?fileType=xml")

    def get_plate_list(self, ip, user, password):
        """Devuelve el arbol XML de la lista actual de la camara."""
        resp = self._request("GET", self._list_url(ip), user, password)
        if resp.status_code == 401:
            raise AnprCameraError("CAMERA_AUTH", "digest auth rechazada")
        if resp.status_code != 200:
            raise AnprCameraError("CAMERA_ERROR", f"http_{resp.status_code} en export")
        try:
            return ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise AnprCameraError("CAMERA_ERROR", f"export XML invalido: {e}")

    def put_plate_list(self, ip, user, password, tree, timeout=ISAPI_TIMEOUT):
        """Importa (reemplaza) la lista completa. Detecta overLimit explicito."""
        body = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
        resp = self._request("PUT", self._list_url(ip), user, password,
                             timeout=timeout, data=body,
                             headers={"Content-Type": "application/xml"})
        if resp.status_code == 401:
            raise AnprCameraError("CAMERA_AUTH", "digest auth rechazada")
        text = resp.text or ""
        # El firmware responde ImportResult/ResponseStatus; "overLimit" es el
        # unico error que el proyecto exige distinguir (lista llena).
        if "overLimit" in text:
            raise AnprCameraError("LIST_FULL", "la camara reporto overLimit")
        if resp.status_code != 200 or "importFail" in text or "importErrorData" in text:
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
    # Manipulacion del XML de la lista (namespace-agnostica: se preserva
    # el arbol tal como lo exporto el firmware y solo se tocan entradas)
    # ------------------------------------------------------------------

    @staticmethod
    def _entries(tree):
        return _find_all(tree, "LicensePlateInfo")

    @staticmethod
    def _entry_parent(tree):
        """Elemento que contiene las LicensePlateInfo (LicensePlateInfoList),
        o el root si el firmware las cuelga directo."""
        for e in tree.iter():
            if _local(e.tag) == "LicensePlateInfoList":
                return e
        return tree

    @staticmethod
    def _entry_plate(entry):
        return _normalize(_find_child_text(entry, "LicensePlate"))

    @classmethod
    def _build_entry(cls, tree, plate_normalized):
        """Crea una LicensePlateInfo nueva. Clona la estructura de una entrada
        existente (se adapta al firmware); si la lista esta vacia usa la forma
        canonica de la guia de la serie 7."""
        entries = cls._entries(tree)
        if entries:
            entry = copy.deepcopy(entries[0])
            _set_child_text(entry, "LicensePlate", plate_normalized)
            _set_child_text(entry, "id", "")
            _set_child_text(entry, "createTime",
                            datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
            return entry
        ns = ""
        root_tag = tree.tag if isinstance(tree.tag, str) else ""
        if "}" in root_tag:
            ns = root_tag.split("}")[0] + "}"
        entry = ET.Element(f"{ns}LicensePlateInfo")
        for name, value in (
            ("id", ""),
            ("LicensePlate", plate_normalized),
            ("type", "whitelist"),
            ("createTime", datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
        ):
            child = ET.SubElement(entry, f"{ns}{name}")
            child.text = value
        return entry

    @classmethod
    def _renumber(cls, tree):
        for i, entry in enumerate(cls._entries(tree), start=1):
            _set_child_text(entry, "id", str(i))

    def apply_plate(self, ip, user, password, action, plate_normalized):
        """ADD/DELETE idempotente de UNA placa via read-modify-write.
        Devuelve el detail del contrato."""
        tree = self.get_plate_list(ip, user, password)
        parent = self._entry_parent(tree)
        existing = [e for e in self._entries(tree)
                    if self._entry_plate(e) == plate_normalized]

        if action == "ADD":
            if existing:
                return "already_present_noop"
            parent.append(self._build_entry(tree, plate_normalized))
            self._renumber(tree)
            self.put_plate_list(ip, user, password, tree)
            return "created"

        # DELETE
        if not existing:
            return "not_found_noop"
        for entry in existing:
            parent.remove(entry)
        self._renumber(tree)
        self.put_plate_list(ip, user, password, tree)
        return "deleted"

    def replace_plates(self, ip, user, password, plates_normalized):
        """Full sync: deja la camara EXACTAMENTE con `plates_normalized`."""
        capacity = self.plate_capacity(ip, user, password)
        if capacity is not None and len(plates_normalized) > capacity:
            raise AnprCameraError(
                "LIST_FULL",
                f"{len(plates_normalized)} placas > capacidad {capacity}")
        tree = self.get_plate_list(ip, user, password)
        parent = self._entry_parent(tree)
        template_tree = tree  # las entradas viejas sirven de plantilla
        for entry in list(self._entries(tree)):
            parent.remove(entry)
        seen = set()
        for plate in plates_normalized:
            if not plate or plate in seen:
                continue
            seen.add(plate)
            parent.append(self._build_entry(template_tree, plate))
            template_tree = tree
        self._renumber(tree)
        self.put_plate_list(ip, user, password, tree, timeout=ISAPI_SYNC_TIMEOUT)
        return len(seen)


class AnprService:
    """Fan-out de operaciones de placas a las camaras del request.

    En la practica el backend manda UNA camara por request, pero `cameras`
    es array por contrato (simetria con terminals de /update-card)."""

    def __init__(self, channel=DEFAULT_CHANNEL):
        self.client = AnprCameraClient(channel=channel)

    @staticmethod
    def _targets(cameras):
        for camera in cameras or []:
            ip = (camera.get("ip") or "").strip()
            if not ip:
                continue
            yield ip, camera.get("user") or "admin", camera.get("password") or ""

    def update_plate(self, action, plate_normalized, cameras):
        """Devuelve (results, error_code): error_code None si todo OK."""
        results, error_code = [], None
        for ip, user, password in self._targets(cameras):
            try:
                detail = self.client.apply_plate(ip, user, password,
                                                 action, plate_normalized)
                results.append({"ip": ip, "ok": True, "detail": detail})
            except AnprCameraError as e:
                log.error("update-plate %s %s fallo en %s: %s",
                          action, plate_normalized, ip, e.code)
                results.append({"ip": ip, "ok": False, "detail": e.code})
                error_code = _worst(error_code, e.code)
        return results, error_code

    def sync_plates(self, plates_normalized, cameras):
        results, error_code = [], None
        for ip, user, password in self._targets(cameras):
            try:
                applied = self.client.replace_plates(ip, user, password,
                                                     plates_normalized)
                results.append({"ip": ip, "ok": True,
                                "detail": f"replaced_{applied}"})
            except AnprCameraError as e:
                log.error("sync-plates (%s placas) fallo en %s: %s",
                          len(plates_normalized), ip, e.code)
                results.append({"ip": ip, "ok": False, "detail": e.code})
                error_code = _worst(error_code, e.code)
        return results, error_code


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
