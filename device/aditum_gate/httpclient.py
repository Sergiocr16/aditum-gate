"""Sesion HTTP compartida con reintentos y timeouts por defecto."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = (3, 10)  # (connect, read)

_session = None


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
