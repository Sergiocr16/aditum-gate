"""Mantenimiento remoto: reiniciar los servicios o el equipo entero.

Los dos comandos matan al proceso que los lanza (PM2 reinicia aditum-device;
el reboot se lleva todo), asi que se disparan desacoplados: sin
start_new_session el comando es hijo de aditum-device y muere con el —PM2 le
manda SIGINT al grupo entero antes de relanzarlo— y la orden se pierde justo
al ejecutarla. El caller responde el HTTP primero y agenda esto con un Timer,
igual que /restart.

Nada de esto reemplaza al watchdog de red (watchdog.py), que reinicia solo
cuando pierde conectividad: aca la orden viene de afuera.
"""
import logging
import os
import shutil
import subprocess

from .health import PM2_PROCESS_NAMES

log = logging.getLogger("aditum.maintenance")

# Bajo PM2/systemd el PATH es minimo, asi que buscar tambien donde npm -g y
# el sistema los dejan (watchdog.py usa /sbin/reboot por la misma razon).
PM2_PATHS = ("/usr/local/bin/pm2", "/usr/bin/pm2", "/usr/lib/node_modules/pm2/bin/pm2")
REBOOT_PATHS = ("/sbin/reboot", "/usr/sbin/reboot")


def _resolve(name, candidates):
    found = shutil.which(name)
    if found:
        return found
    for path in candidates:
        if os.access(path, os.X_OK):
            return path
    return None


def locate_pm2():
    return _resolve("pm2", PM2_PATHS)


def locate_reboot():
    return _resolve("reboot", REBOOT_PATHS)


def _spawn(argv):
    """Lanza el comando desacoplado y no espera (el hijo nos va a matar)."""
    # PM2 corre como root; el sudo es solo para una instalacion atipica.
    # -n en vez de interactivo: sin password falla en vez de colgarse.
    if os.geteuid() != 0:
        argv = ["sudo", "-n"] + argv
    try:
        subprocess.Popen(
            argv, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as e:
        log.error("No se pudo ejecutar %s: %s", argv[0], e)
        return False


def restart_services():
    """Reinicia los dos procesos PM2 (device + web). El SO no se toca."""
    pm2 = locate_pm2()
    if not pm2:
        log.error("pm2 no encontrado: no se pueden reiniciar los servicios")
        return False
    log.warning("Reiniciando servicios PM2: %s", ", ".join(PM2_PROCESS_NAMES))
    return _spawn([pm2, "restart"] + list(PM2_PROCESS_NAMES))


def reboot_system():
    """Reinicia el equipo. El acceso queda caido hasta que vuelva a bootear."""
    binary = locate_reboot()
    if not binary:
        log.error("No se encontro el binario de reboot")
        return False
    log.warning("Reiniciando el EQUIPO por pedido remoto")
    return _spawn([binary])
