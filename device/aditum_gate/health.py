"""Reporte de salud del dispositivo para el editor local /admin.

Junta en un solo JSON: dispositivos de entrada USB conectados (lo que antes
se veia con `sudo evtest`), el estado de los lectores configurados, el estado
de los servicios (PM2 y el server web :3000) y metricas basicas del sistema.
Cada bloque falla de forma aislada: un error en uno no tumba el reporte.
"""
import json
import logging
import os
import subprocess
import time

from .httpclient import request

log = logging.getLogger("aditum.health")

WEB_SERVER_URL = "http://localhost:3000/api/config"
PM2_PROCESS_NAMES = ("aditum-device", "aditum-web")

# Nombre legible de cada servicio (lo que muestra la vista de Salud)
SERVICE_LABELS = {
    "aditum-device": "Controlador",
    "aditum-web": "Pantalla / WebSocket",
    "web-server-3000": "Server web :3000",
}


def list_input_devices():
    """Todos los input devices con handler eventX (formato de evtest)."""
    devices = []
    try:
        with open("/proc/bus/input/devices") as f:
            lines = f.readlines()
    except OSError as e:
        log.warning("No se pudo leer /proc/bus/input/devices: %s", e)
        return devices

    current_name = None
    current_handlers = None
    for line in lines + [""]:
        line = line.strip()
        if line.startswith("N: Name="):
            current_name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            current_handlers = line.split("=", 1)[1].strip()
        elif line == "":
            if current_name and current_handlers:
                for token in current_handlers.split():
                    if token.startswith("event"):
                        devices.append({
                            "path": f"/dev/input/{token}",
                            "name": current_name,
                        })
            current_name = None
            current_handlers = None
    return devices


def list_cameras():
    """Camaras USB de captura: menor /dev/videoN por dispositivo fisico.

    Se filtra por bus USB para excluir los codecs del SoC (bcm2835-*); una
    webcam expone varios nodos (captura + metadata) con el mismo padre, el
    de menor indice es el de captura.
    """
    base = "/sys/class/video4linux"
    by_parent = {}
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    for entry in entries:
        if not entry.startswith("video"):
            continue
        try:
            index = int(entry[len("video"):])
        except ValueError:
            continue
        real = os.path.realpath(os.path.join(base, entry))
        if "/usb" not in real:
            continue
        try:
            with open(os.path.join(base, entry, "name")) as f:
                name = f.read().strip()
        except OSError:
            name = entry
        parent = real.rsplit("/video4linux", 1)[0]
        current = by_parent.get(parent)
        if current is None or index < current["index"]:
            by_parent[parent] = {"index": index, "name": name}
    return sorted(by_parent.values(), key=lambda c: c["index"])


def readers_status(settings, input_devices):
    """Cruza los lectores configurados con el hardware detectado."""
    readers = []
    for reader in settings.scanners:
        entry = {"role": reader.role, "doorId": reader.door_id}
        if settings.scanner_type == "hid":
            wanted = (reader.device_name or "").lower()
            matches = [d for d in input_devices
                       if wanted and wanted in d["name"].lower()]
            entry["deviceName"] = reader.device_name
            entry["connected"] = bool(matches)
            entry["paths"] = [d["path"] for d in matches]
        elif settings.scanner_type == "opencv":
            index = reader.camera_index if reader.camera_index is not None else 0
            path = f"/dev/video{index}"
            entry["cameraIndex"] = index
            entry["connected"] = os.path.exists(path)
            entry["paths"] = [path] if entry["connected"] else []
        else:
            # hikvision/none: no hay lector local que verificar
            entry["connected"] = None
            entry["paths"] = []
        readers.append(entry)
    return readers


def _pm2_snapshot():
    """Estado de los procesos PM2 via `pm2 jlist` (read-only).

    Devuelve dict nombre -> {status, restarts, uptimeSec, pid} o None si
    PM2 no esta disponible (banco de dev sin PM2, timeout, etc.).
    """
    try:
        result = subprocess.run(
            ["pm2", "jlist"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning("pm2 jlist fallo (rc=%s): %s",
                        result.returncode, result.stderr.strip()[:200])
            return None
        # pm2 puede anteponer lineas de log al JSON: buscar el arranque
        stdout = result.stdout
        start = stdout.find("[")
        if start < 0:
            return None
        processes = json.loads(stdout[start:])
    except Exception as e:
        log.warning("No se pudo consultar PM2: %s", e)
        return None

    snapshot = {}
    for proc in processes:
        env = proc.get("pm2_env", {})
        uptime_ms = env.get("pm_uptime")
        uptime_sec = None
        if isinstance(uptime_ms, (int, float)) and env.get("status") == "online":
            uptime_sec = max(0, int(time.time() - uptime_ms / 1000.0))
        snapshot[proc.get("name")] = {
            "status": env.get("status", "unknown"),
            "restarts": env.get("restart_time"),
            "uptimeSec": uptime_sec,
            "pid": proc.get("pid") or None,
        }
    return snapshot


def services_status(settings):
    """Estado de los procesos y del server web :3000.

    Sin pantalla, aditum-web y el server :3000 no cumplen funcion visible
    para ese equipo: se omiten del reporte (no aplican).
    """
    pm2 = _pm2_snapshot()
    services = []
    names = PM2_PROCESS_NAMES if settings.has_screen else ("aditum-device",)

    for name in names:
        info = {"name": name, "label": SERVICE_LABELS.get(name, name)}
        if name == "aditum-device":
            # Si respondemos este request, el proceso esta vivo por definicion
            info["online"] = True
            info["pid"] = os.getpid()
        if pm2 is not None:
            proc = pm2.get(name)
            if proc:
                info.update(proc)
                if "online" not in info:
                    info["online"] = proc["status"] == "online"
            elif "online" not in info:
                info["online"] = False
                info["status"] = "missing"
        services.append(info)

    if settings.has_screen:
        # El check real de aditum-web es HTTP: PM2 puede decir online con el
        # puerto muerto. Se reporta aparte para distinguir proceso vs servicio.
        web = {"name": "web-server-3000",
               "label": SERVICE_LABELS["web-server-3000"]}
        try:
            resp = request("GET", WEB_SERVER_URL, timeout=(2, 5))
            web["online"] = resp.status_code == 200
            web["httpStatus"] = resp.status_code
        except Exception as e:
            web["online"] = False
            web["error"] = str(e)[:200]
        services.append(web)

        # aditum-web sin dato de PM2: usar el check HTTP como veredicto
        for svc in services:
            if svc["name"] == "aditum-web" and "online" not in svc:
                svc["online"] = web["online"]

    return {"services": services, "pm2Available": pm2 is not None}


def system_status():
    """Metricas basicas del Pi; cada lectura falla por separado."""
    system = {"cpuTempC": None, "uptimeSec": None,
              "memAvailableMb": None, "memTotalMb": None}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            system["cpuTempC"] = round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError) as e:
        log.debug("Sin temperatura de CPU: %s", e)
    try:
        with open("/proc/uptime") as f:
            system["uptimeSec"] = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError) as e:
        log.debug("Sin uptime: %s", e)
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts and parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
                    mem[parts[0].rstrip(":")] = int(parts[1])
        if "MemTotal" in mem:
            system["memTotalMb"] = mem["MemTotal"] // 1024
        if "MemAvailable" in mem:
            system["memAvailableMb"] = mem["MemAvailable"] // 1024
    except (OSError, ValueError) as e:
        log.debug("Sin memoria: %s", e)
    return system


def report(settings):
    """Arma el reporte completo para GET /health."""
    input_devices = list_input_devices()
    services = services_status(settings)
    return {
        "services": services["services"],
        "pm2Available": services["pm2Available"],
        "inputDevices": input_devices,
        "cameras": list_cameras(),
        "readers": readers_status(settings, input_devices),
        "system": system_status(),
        "kioskExpected": bool(settings.has_screen),
    }
