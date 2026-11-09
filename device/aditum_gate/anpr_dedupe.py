"""Filtro de lecturas repetidas de la misma placa (ventana corta, por camara).

Las camaras ANPR traian un filtro propio que no volvia a reportar una placa
durante 1 minuto. Se quito a proposito: un carro que da vuelta en U y vuelve
a pasar por la aguja dentro de ese minuto tiene que leerse de nuevo. El costo
es que ahora la camara reporta la MISMA pasada mas de una vez cuando el carro
queda unos segundos en la zona de deteccion (frena en la aguja, avanza un
poco, la camara vuelve a disparar). Sin este filtro cada una de esas lecturas
llegaria a Aditum como un evento distinto de la bitacora.

Reglas:
  - Se colapsan por (camara, placa normalizada): dos camaras del mismo Pi
    (entrada y salida) son eventos distintos aunque lean la misma placa. La
    placa se compara normalizada ([A-Z0-9]) porque el OCR puede alternar
    "ABC-123" y "ABC123" entre un disparo y el siguiente.
  - Ventana DESLIZANTE: cada lectura repetida renueva el reloj, asi que un
    carro detenido en la aguja que se lee cada 5 s durante medio minuto es UN
    solo evento. La vuelta en U sigue contando: entre una pasada y la otra hay
    un silencio mayor que la ventana.
  - Solo cuenta lo que se habria enviado a Aditum: el caller lo aplica DESPUES
    del filtro de allowlist, asi que una lectura descartada no arma la ventana
    y una placa recien agregada a la lista entra con su primera lectura
    autorizada.
  - En memoria, no en SQLite: un reinicio del proceso olvida la ventana y lo
    peor que pasa es UN duplicado. No vale la pena persistirlo.
  - Ventana 0 = apagado (comportamiento anterior: se encola todo).
"""
import threading
import time

from .anpr import camera_host, normalize_plate


class RecentPlateFilter:
    """Recuerda cuando se leyo por ultima vez cada placa en cada camara."""

    def __init__(self, window_seconds, clock=time.monotonic):
        try:
            self.window_seconds = max(0, int(window_seconds or 0))
        except (TypeError, ValueError):
            self.window_seconds = 0
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen = {}   # (camara, placa normalizada) -> instante de la ultima lectura

    @property
    def enabled(self):
        return self.window_seconds > 0

    @staticmethod
    def _key(plate, source_ip):
        normalized = normalize_plate(plate) or (plate or "").strip().upper()
        return camera_host(source_ip), normalized

    def repeated_within(self, plate, source_ip):
        """Registra la lectura y dice si es repetida.

        Devuelve None si es la primera lectura de esa placa por esa camara
        dentro de la ventana (hay que encolarla) o, si es repetida, los
        segundos transcurridos desde la lectura anterior. En los dos casos la
        lectura renueva la ventana."""
        if not self.enabled:
            return None
        key = self._key(plate, source_ip)
        now = self._clock()
        with self._lock:
            self._forget_old(now)
            last = self._last_seen.get(key)
            self._last_seen[key] = now
        if last is None:
            return None
        return now - last

    def _forget_old(self, now):
        # Lo que ya salio de la ventana no sirve para nada: asi el diccionario
        # solo tiene las placas vistas en los ultimos segundos.
        cutoff = now - self.window_seconds
        for key in [k for k, seen in self._last_seen.items() if seen < cutoff]:
            del self._last_seen[key]

    def tracked(self):
        """Cuantas (camara, placa) estan dentro de la ventana ahora mismo."""
        with self._lock:
            self._forget_old(self._clock())
            return len(self._last_seen)
