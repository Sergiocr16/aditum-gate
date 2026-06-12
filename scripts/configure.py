#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wizard de configuracion del dispositivo Aditum Gate.

Genera config-runtime.json (validado contra config.schema.json) +
device-id.txt (+ device-token.txt opcional) partiendo de las plantillas de
examples/. Lo invoca scripts/bootstrap.sh, pero puede correrse a mano:

  .venv/bin/python3 scripts/configure.py                    # interactivo
  .venv/bin/python3 scripts/configure.py --hints DIR        # con defaults
                                                            # cosechados de la
                                                            # instalacion vieja
  .venv/bin/python3 scripts/configure.py --from-file c.json # config preparada
  .venv/bin/python3 scripts/configure.py --non-interactive  # por env vars

Env vars del modo no interactivo: ADITUM_DEVICE_ID (obligatoria),
ADITUM_PLACE_NAME, ADITUM_SCANNER_TYPE (hid|opencv|hikvision|none),
ADITUM_API (app|caseta), ADITUM_VERIFIER (secure|legacy), ADITUM_HAS_SCREEN
(1|0), ADITUM_DOOR_TYPE (ENTRY|EXIT), ADITUM_LOGO_URL, ADITUM_SCANNERS
(JSON array), ADITUM_WATCHDOG (1|0), ADITUM_NEOPIXEL (1|0), ADITUM_TOKEN.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = REPO_ROOT / "config.schema.json"
RUNTIME_FILE = REPO_ROOT / "config-runtime.json"
DEVICE_ID_FILE = REPO_ROOT / "device-id.txt"
DEVICE_TOKEN_FILE = REPO_ROOT / "device-token.txt"

TEMPLATES = {
    "hid": "config-hid-dos-lectores-pantalla.json",
    "opencv": "config-opencv-legacy.json",
    "hikvision": "config-hikvision-solo-portones.json",
    "none": "config-solo-portones.json",
}

API_HOSTS = {
    "app": "https://app.aditumcr.com/api",
    "caseta": "https://caseta.aditumcr.com/api",
}

# ------------------------------------------------------------------
# Entrada/salida
# ------------------------------------------------------------------
_tty = None


def ask(prompt, default=None, hint_source=None):
    """Pregunta por consola usando /dev/tty si stdin no es interactivo
    (caso curl | sudo bash)."""
    global _tty
    suffix = ""
    if default not in (None, ""):
        suffix = f" [{default}" + (f" — detectado de {hint_source}" if hint_source else "") + "]"
    text = f"{prompt}{suffix}: "

    if sys.stdin.isatty():
        answer = input(text)
    else:
        if _tty is None:
            try:
                _tty = open("/dev/tty", "r+")
            except OSError:
                sys.exit("Sin terminal interactiva: usar --non-interactive o --from-file")
        _tty.write(text)
        _tty.flush()
        answer = _tty.readline().rstrip("\n")
    answer = answer.strip()
    return answer if answer else (default if default is not None else "")


def ask_required(prompt, default=None, hint_source=None):
    while True:
        value = ask(prompt, default, hint_source)
        if value:
            return value
        print("  (obligatorio)")


def ask_yes_no(prompt, default=True):
    suffix = "S/n" if default else "s/N"
    value = ask(f"{prompt} ({suffix})", "").lower()
    if not value:
        return default
    return value in ("s", "si", "sí", "y", "yes", "1")


def ask_choice(prompt, options, default_key=None):
    """options: lista de (key, etiqueta). Devuelve la key elegida."""
    print(f"\n{prompt}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    default_idx = next((i for i, (k, _) in enumerate(options, 1) if k == default_key), None)
    while True:
        value = ask("Opcion", str(default_idx) if default_idx else None)
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value) - 1][0]
        print("  Opcion invalida")


# ------------------------------------------------------------------
# Hints de la instalacion vieja (--hints DIR)
# ------------------------------------------------------------------
HINT_PATTERN = re.compile(
    r"""^\s*(doorId|doorType|placeName|hasScreen|showCameraFeed|DEVICE_NAME)"""
    r"""\s*=\s*(['"]?)(.*?)\2\s*(#.*)?$""", re.MULTILINE)

# que archivo viejo describe a que lector
HINT_FILES = {
    "scannerQr.py": ("hid", "entry"),
    "scannerQrExit.py": ("hid", "exit"),
    "scanner.py": ("opencv", "exit"),
    "scannerExit.py": ("opencv", "exit"),
}


def harvest_hints(hints_dir):
    """Lee los scripts viejos cosechados por bootstrap y devuelve
    {archivo: {variable: valor}}. Solo defaults sugeridos; un humano confirma."""
    hints = {}
    if not hints_dir:
        return hints
    for name in HINT_FILES:
        path = Path(hints_dir) / name
        if not path.exists():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        values = {}
        for match in HINT_PATTERN.finditer(text):
            values[match.group(1)] = match.group(3).strip()
        if values:
            hints[name] = values
    return hints


def hint(hints, file_name, var):
    value = hints.get(file_name, {}).get(var)
    return (value, file_name) if value else (None, None)


# ------------------------------------------------------------------
# Deteccion de hardware local (solo en el Pi; en dev no lista nada)
# ------------------------------------------------------------------
def list_hid_devices():
    """Nombres de dispositivos de input (mismo origen que scanners/hid.py)."""
    names = []
    try:
        with open("/proc/bus/input/devices") as f:
            for line in f:
                if line.startswith("N: Name="):
                    name = line.split("=", 1)[1].strip().strip('"')
                    if name and name not in names:
                        names.append(name)
    except OSError:
        pass
    return names


def list_cameras():
    import glob
    return sorted(int(p.replace("/dev/video", "")) for p in glob.glob("/dev/video[0-9]*"))


# ------------------------------------------------------------------
# Wizard
# ------------------------------------------------------------------
def load_template(scanner_type):
    with open(REPO_ROOT / "examples" / TEMPLATES[scanner_type]) as f:
        return json.load(f)


def wizard(hints_dir=None):
    hints = harvest_hints(hints_dir)
    if hints:
        print("Se detectaron valores de la instalacion anterior; se ofrecen "
              "como defaults (confirmar cada uno).")

    print("\n=== Aditum Gate: configuracion del dispositivo ===")
    device_id = ask_required("ID del dispositivo (ej. GATE-CR-0034)")
    h_place, h_src = hint(hints, "scannerQr.py", "placeName")
    if not h_place:
        h_place, h_src = hint(hints, "scanner.py", "placeName")
    place_name = ask_required("Nombre del lugar/condominio", h_place, h_src)

    default_type = "hid" if "scannerQr.py" in hints else ("opencv" if "scanner.py" in hints else None)
    scanner_type = ask_choice("Tipo de instalacion", [
        ("hid", "Lectores QR pistola (HID)"),
        ("opencv", "Camaras USB (OpenCV)"),
        ("hikvision", "Terminales Hikvision (sin scanner local)"),
        ("none", "Solo apertura de portones"),
    ], default_type)

    config = load_template(scanner_type)
    config["deviceId"] = device_id
    config["placeName"] = place_name
    config["configRevision"] = 1

    api = ask_choice("Backend de Aditum", [
        ("app", "app.aditumcr.com"),
        ("caseta", "caseta.aditumcr.com"),
    ], "caseta" if scanner_type == "hid" else "app")
    config["api"]["baseUrl"] = API_HOSTS[api]
    config["api"]["verifierStyle"] = ask_choice("Estilo de verificacion QR", [
        ("secure", "secure (prefijo ADTG, QR firmado) — instalaciones nuevas"),
        ("legacy", "legacy (prefijo ADITUMGATE=) — instalaciones viejas"),
    ], config["api"].get("verifierStyle", "secure"))

    # Lectores
    if scanner_type in ("hid", "opencv"):
        count = int(ask_choice("Cantidad de lectores/camaras", [("1", "1"), ("2", "2")],
                               "2" if len(config.get("scanners", [])) == 2 else "1"))
        devices = list_hid_devices() if scanner_type == "hid" else []
        cameras = list_cameras() if scanner_type == "opencv" else []
        config["scanners"] = []
        for i in range(count):
            role_default = "entry" if i == 0 else "exit"
            print(f"\n--- Lector {i + 1} ---")
            role = ask_choice("Rol", [("entry", "entrada"), ("exit", "salida")], role_default)
            hint_file = ("scannerQr.py" if role == "entry" else "scannerQrExit.py") \
                if scanner_type == "hid" else ("scannerExit.py" if i else "scanner.py")
            h_door, h_dsrc = hint(hints, hint_file, "doorId")
            reader = {"role": role, "doorId": ask_required("doorId (de Aditum)", h_door, h_dsrc)}
            if scanner_type == "hid":
                h_name, h_nsrc = hint(hints, hint_file, "DEVICE_NAME")
                if devices:
                    print("Dispositivos HID detectados:")
                    for j, name in enumerate(devices, 1):
                        print(f"  {j}) {name}")
                    value = ask("Numero o nombre del lector", h_name, h_nsrc)
                    reader["deviceName"] = devices[int(value) - 1] \
                        if value.isdigit() and 1 <= int(value) <= len(devices) else value
                else:
                    reader["deviceName"] = ask_required(
                        "Nombre del lector HID (cat /proc/bus/input/devices)", h_name, h_nsrc)
            else:
                if cameras:
                    print(f"Camaras detectadas: {cameras}")
                reader["cameraIndex"] = int(ask_required(
                    "Indice de camara (/dev/videoN)", str(cameras[i]) if i < len(cameras) else "0"))
                reader["showCameraFeed"] = ask_yes_no("Mostrar ventana de camara (solo debug)", False)
            config["scanners"].append(reader)
    else:
        config["scanners"] = []

    # Pantalla
    h_screen = hints.get("scannerQr.py", {}).get("hasScreen")
    has_screen = ask_yes_no("\n¿Este equipo tiene pantalla pedestal?",
                            h_screen == "True" if h_screen else config["screen"]["hasScreen"])
    config["screen"] = {"hasScreen": has_screen}
    if has_screen:
        config["screen"]["doorType"] = ask_choice("La pantalla corresponde a", [
            ("ENTRY", "entrada"), ("EXIT", "salida")], "ENTRY")
        logo = ask("URL del logo del cliente (Cloudinary)", "")
        if logo:
            config["screen"]["clientLogoUrl"] = logo

    # GPIO y extras
    if not ask_yes_no("\n¿Usar los pines GPIO por defecto de la plantilla?", True):
        pairs = ask_required("Pares id:pin separados por coma (ej. 1:7,2:11)")
        config["gpio"]["gates"] = [
            {"id": int(p.split(":")[0]), "pin": int(p.split(":")[1])}
            for p in pairs.split(",")
        ]
    config["gpio"].setdefault("watchdog", {})["enabled"] = \
        ask_yes_no("¿Watchdog de red (reboot si pierde internet)?", True)
    config["gpio"].setdefault("neopixel", {})["enabled"] = \
        ask_yes_no("¿Tira LED NeoPixel?", False)
    if scanner_type == "hikvision":
        config["hikvision"]["enabled"] = True

    token = ask("\nToken del dispositivo (Enter para provisionarlo despues via PUT /token)", "")
    return config, device_id, token


# ------------------------------------------------------------------
# Validacion y escritura
# ------------------------------------------------------------------
def validate(config):
    try:
        import jsonschema
    except ImportError:
        sys.exit("Falta jsonschema (correr scripts/bootstrap.sh para crear el venv)")
    with open(SCHEMA_FILE) as f:
        schema = json.load(f)
    return [e.message for e in jsonschema.Draft7Validator(schema).iter_errors(config)]


def write_atomic(path, content, mode=None):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)
    if mode is not None:
        os.chmod(path, mode)


def persist(config, device_id, token):
    write_atomic(RUNTIME_FILE, json.dumps(config, indent=2) + "\n")
    write_atomic(DEVICE_ID_FILE, device_id + "\n")
    if token:
        write_atomic(DEVICE_TOKEN_FILE, token + "\n", mode=0o600)
    print(f"\nEscrito: {RUNTIME_FILE.name}, {DEVICE_ID_FILE.name}"
          + (f", {DEVICE_TOKEN_FILE.name}" if token else ""))


# ------------------------------------------------------------------
# Modo no interactivo
# ------------------------------------------------------------------
def from_env():
    missing = [v for v in ("ADITUM_DEVICE_ID",) if not os.environ.get(v)]
    if missing:
        print(f"Faltan variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    env = os.environ.get
    scanner_type = env("ADITUM_SCANNER_TYPE", "none")
    if scanner_type not in TEMPLATES:
        sys.exit(f"ADITUM_SCANNER_TYPE invalido: {scanner_type}")
    config = load_template(scanner_type)
    config["deviceId"] = env("ADITUM_DEVICE_ID")
    config["configRevision"] = 1
    if env("ADITUM_PLACE_NAME"):
        config["placeName"] = env("ADITUM_PLACE_NAME")
    if env("ADITUM_API"):
        config["api"]["baseUrl"] = API_HOSTS[env("ADITUM_API")]
    if env("ADITUM_VERIFIER"):
        config["api"]["verifierStyle"] = env("ADITUM_VERIFIER")
    if env("ADITUM_SCANNERS"):
        config["scanners"] = json.loads(env("ADITUM_SCANNERS"))
    if env("ADITUM_HAS_SCREEN") is not None and env("ADITUM_HAS_SCREEN") != "":
        config["screen"]["hasScreen"] = env("ADITUM_HAS_SCREEN") == "1"
    if env("ADITUM_DOOR_TYPE"):
        config["screen"]["doorType"] = env("ADITUM_DOOR_TYPE")
    if env("ADITUM_LOGO_URL"):
        config["screen"]["clientLogoUrl"] = env("ADITUM_LOGO_URL")
    if env("ADITUM_WATCHDOG") is not None and env("ADITUM_WATCHDOG") != "":
        config["gpio"].setdefault("watchdog", {})["enabled"] = env("ADITUM_WATCHDOG") == "1"
    if env("ADITUM_NEOPIXEL") is not None and env("ADITUM_NEOPIXEL") != "":
        config["gpio"].setdefault("neopixel", {})["enabled"] = env("ADITUM_NEOPIXEL") == "1"
    return config, config["deviceId"], env("ADITUM_TOKEN", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hints", help="directorio con los scripts viejos cosechados")
    parser.add_argument("--from-file", help="config JSON ya preparada")
    parser.add_argument("--device-id", help="con --from-file: deviceId a escribir")
    parser.add_argument("--token", help="token del dispositivo (opcional)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="construir la config desde variables ADITUM_*")
    args = parser.parse_args()

    if args.from_file:
        with open(args.from_file) as f:
            config = json.load(f)
        device_id = args.device_id or config.get("deviceId")
        if not device_id:
            sys.exit("--from-file requiere deviceId en el JSON o --device-id")
        config["deviceId"] = device_id
        token = args.token or os.environ.get("ADITUM_TOKEN", "")
    elif args.non_interactive:
        config, device_id, token = from_env()
    else:
        while True:
            config, device_id, token = wizard(args.hints)
            errors = validate(config)
            if not errors:
                break
            print("\nLa configuracion no valida contra el schema:")
            for e in errors:
                print(f"  - {e}")
            print("Revisemos de nuevo.\n")
        print("\n=== Configuracion resultante ===")
        print(json.dumps(config, indent=2))
        if not ask_yes_no("¿Confirmar y guardar?", True):
            sys.exit("Cancelado.")
        persist(config, device_id, token)
        return

    errors = validate(config)
    if errors:
        print("Config invalida:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    persist(config, device_id, token)


if __name__ == "__main__":
    main()
