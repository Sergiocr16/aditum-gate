"""Sesiones HTTP compartidas con timeouts por defecto.

Dos sesiones distintas:
  - get_session()/request(): hacia el backend de Aditum, con reintentos
    (502/503/504 y errores de conexion) porque ahi un reintento es barato.
  - get_isapi_session()/isapi_request(): hacia los terminales Hikvision en
    la LAN, SIN reintentos (ver docstring de get_isapi_session).
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError, ProtocolError
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = (3, 10)  # (connect, read)

_session = None
_isapi_session = None


def get_session():
    global _session
    if _session is None:
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "PUT"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        _session = requests.Session()
        _session.mount("http://", adapter)
        _session.mount("https://", adapter)
    return _session


def request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs):
    return get_session().request(method, url, timeout=timeout, **kwargs)


def get_isapi_session():
    """Sesion para las llamadas ISAPI a los terminales Hikvision, sin reintentos.

    Por que max_retries=0:
      - Los terminales no devuelven 502/503/504: responden ellos mismos, no
        hay proxy adelante que justifique el status_forcelist.
      - El 401 del digest lo resuelve `requests` (HTTPDigestAuth reenvia con
        el nonce), no urllib3; el adapter no participa en eso.
      - Un reintento ciego sobre un terminal apagado cuesta connect_timeout
        por intento mas backoff (~15 s con el adapter del backend), y el
        telefono esta esperando la respuesta de /update-card.
      - Reintentar un 401 real alimenta el bloqueo por login ilegal del
        Hikvision (~7 intentos fallidos -> 30 min sin acceso al terminal).
    """
    global _isapi_session
    if _isapi_session is None:
        adapter = HTTPAdapter(max_retries=0)
        _isapi_session = requests.Session()
        _isapi_session.mount("http://", adapter)
        _isapi_session.mount("https://", adapter)
    return _isapi_session


def isapi_request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs):
    """Misma firma que request(), pero por la sesion ISAPI (sin reintentos de
    urllib3). Unica excepcion: si la conexion keep-alive del pool ya la habia
    cerrado el terminal (ProtocolError / RemoteDisconnected al reusarla, una
    carrera real con los Hikvision embebidos que cierran sockets ociosos) se
    reintenta UNA vez con conexion nueva. Un terminal apagado (connect
    timeout, host inalcanzable) o un read timeout NO se reintentan: cuestan
    tiempo y el telefono esta esperando.
    """
    session = get_isapi_session()
    try:
        return session.request(method, url, timeout=timeout, **kwargs)
    except requests.ConnectionError as exc:
        if not _is_stale_keepalive(exc):
            raise
        return session.request(method, url, timeout=timeout, **kwargs)


def _is_stale_keepalive(exc):
    """ConnectionError cuya causa es un ProtocolError de urllib3 ('Connection
    aborted', RemoteDisconnected): el socket reusado ya estaba cerrado. Llega
    pelado (POST, metodo no reintentable para urllib3) o envuelto en
    MaxRetryError (PUT, con total=0)."""
    cause = exc.args[0] if exc.args else None
    if isinstance(cause, MaxRetryError):
        cause = cause.reason
    return isinstance(cause, ProtocolError)
