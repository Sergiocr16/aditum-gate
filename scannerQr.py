import evdev
from evdev import ecodes, categorize
import requests
import time

# =========================
# Configuración
# =========================
doorType = "entry"   # "entry" o "exit"
doorId = '0'
placeName = 'Name'
hasScreen = True
DEVICE_NAME = "Newtologic  4010E"  # Nombre (o parte) del lector HID

# Seguridad
REQUIRED_PREFIX = "ADTG"
MAX_CODE_LEN = 256  # evita lecturas anormalmente largas

# =========================
# Utilidades de dispositivoc
# =========================
def find_device_path_by_name(device_name: str):
    """
    Devuelve /dev/input/eventX del dispositivo cuyo nombre contenga 'device_name'
    sin depender de evtest.
    """
    wanted = (device_name or "").lower()
    try:
        with open("/proc/bus/input/devices", "r") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Error al leer /proc/bus/input/devices: {e}")
        return None

    current_name = None
    current_handlers = None

    for line in lines:
        line = line.strip()
        if line.startswith("N: Name="):
            current_name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            current_handlers = line.split("=", 1)[1].strip()
        elif line == "":
            if current_name and current_handlers:
                candidate = (current_name or "").lower()
                if wanted and wanted in candidate:
                    for token in current_handlers.split():
                        if token.startswith("event"):
                            return f"/dev/input/{token}"
            current_name = None
            current_handlers = None
    return None

# =========================
# HTTP helpers (opcionales)
# =========================
def send_request(endpoint, data):
    url = f'https://app.aditumcr.com/api/{endpoint}'
    response = requests.post(url, json=data)
    return response.json()

def send_to_nodejs(endpoint, data=None):
    if hasScreen:
        url = f'http://localhost:3000/{endpoint}'
        response = requests.post(url, json=data)
        return response.json()

def loading():
    if hasScreen:
        try:
            print("LOADING")
            url = f'http://localhost:3000/api/loading'
            response = requests.post(url, json={"name": "loading"})
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            print("Error en la solicitud de loading:", str(e))
            return None

def denied(data=None):
    print("Acceso denegado")
    if not hasScreen:
        return None
    payload = data or {}
    payload.setdefault("doorType", doorType)
    payload.setdefault("doorId", doorId)
    payload.setdefault("placeName", placeName)
    try:
        return send_to_nodejs("api/code-denied", payload)
    except requests.exceptions.RequestException as e:
        print("Error al enviar code-denied a Node:", str(e))
        return None

# =========================
# Lector del dispositivo
# =========================
def read_events(device_path):
    """
    Lee el teclado HID del lector QR. Bloquea inmediatamente la lectura si el texto
    deja de coincidir con el prefijo REQUIRED_PREFIX, y exige '=' justo después.
    """
    dev = evdev.InputDevice(device_path)
    dev.grab()  # control exclusivo para evitar inyección en la terminal

    scancodes = {
        0: None, 1: 'ESC', 2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8',
        10: '9', 11: '0', 12: '-', 13: '=', 14: 'BKSP', 15: 'TAB', 16: 'q', 17: 'w', 18: 'e', 19: 'r',
        20: 't', 21: 'y', 22: 'u', 23: 'i', 24: 'o', 25: 'p', 26: '[', 27: ']', 28: 'CR', 29: 'LCTRL',
        30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h', 36: 'j', 37: 'k', 38: 'l', 39: ';',
        40: '"', 41: '`', 42: 'LSHFT', 43: '\\', 44: 'z', 45: 'x', 46: 'c', 47: 'v', 48: 'b', 49: 'n',
        50: 'm', 51: ',', 52: '.', 53: '/', 54: 'RSHFT', 56: 'LALT', 57: ' ', 100: 'RALT'
    }

    caps_codes = {
        0: None, 1: 'ESC', 2: '!', 3: '@', 4: '#', 5: '$', 6: '%', 7: '^', 8: '&', 9: '*',
        10: '(', 11: ')', 12: '_', 13: '+', 14: 'BKSP', 15: 'TAB', 16: 'Q', 17: 'W', 18: 'E', 19: 'R',
        20: 'T', 21: 'Y', 22: 'U', 23: 'I', 24: 'O', 25: 'P', 26: '{', 27: '}', 28: 'CR', 29: 'LCTRL',
        30: 'A', 31: 'S', 32: 'D', 33: 'F', 34: 'G', 35: 'H', 36: 'J', 37: 'K', 38: 'L', 39: ':',
        40: "'", 41: '~', 42: 'LSHFT', 43: '|', 44: 'Z', 45: 'X', 46: 'C', 47: 'V', 48: 'B', 49: 'N',
        50: 'M', 51: '<', 52: '>', 53: '?', 54: 'RSHFT', 56: 'LALT', 57: ' ', 100: 'RALT'
    }

    # teclas no imprimibles que ignoramos
    NON_CHAR_KEYS = {'LSHFT','RSHFT','CR','BKSP','TAB','ESC','LALT','RALT','LCTRL','RCTRL',' '}
    SHIFT_SCANCODES = {42, 54}

    caps = False
    current_text = ''
    discarding = False  # cuando True, ignoramos todo hasta Enter

    print("Escaneando QR...")

    for event in dev.read_loop():
        if event.type != ecodes.EV_KEY:
            continue

        data = categorize(event)

        # Estado de Shift (izq/der)
        if data.scancode in SHIFT_SCANCODES:
            caps = (data.keystate == 1)
            continue

        # Solo en key down
        if data.keystate != 1:
            continue

        key_lookup = caps_codes.get(data.scancode) if caps else scancodes.get(data.scancode, '')
        if key_lookup is None:
            continue

        # Fin de lectura (Enter)
        if key_lookup == 'CR':
            if discarding:
                # terminamos descarte y esperamos el siguiente código
                discarding = False
                current_text = ''
                continue
            if current_text:
                print("\nCódigo detectado, procesando...")
                return current_text
            continue

        # Ignorar teclas no alfanuméricas/espaciales
        if key_lookup in NON_CHAR_KEYS:
            continue

        # Si estamos descartando, ignorar todo hasta CR
        if discarding:
            continue

        # Agregar caracter y mostrarlo (opcional)
        current_text += key_lookup
        print(key_lookup, end='', flush=True)

        # --- Guardia de prefijo inmediata ---
        typed_len = len(current_text)
        prefix_len = len(REQUIRED_PREFIX)

        # 1) Mientras se escribe el prefijo, debe coincidir (case-insensitive)
        if typed_len <= prefix_len:
            if REQUIRED_PREFIX[:typed_len] != current_text.upper():
                print("\nPrefijo inválido. Bloqueado inmediatamente.")
                denied()
                discarding = True
                current_text = ''
                continue

        # 3) Límite de longitud para mayor seguridad
        if len(current_text) > MAX_CODE_LEN:
            print("\nCódigo demasiado largo. Bloqueado.")
            denied()
            discarding = True
            current_text = ''
            continue

def keylogger(device_path='/dev/input/event0'):
    # Resolver automáticamente el device_path por nombre; si no se encuentra, usar el parámetro por defecto
    resolved = find_device_path_by_name(DEVICE_NAME) or device_path
    return read_events(resolved)

# =========================
# Procesamiento del texto
# =========================
def procesar_texto(texto):
    """
    Valida formato: ADITUMGATE=<dato>
    Bloquea si no cumple; si cumple, llama a los endpoints de verificación.
    """
    texto = texto.strip()

    # Validación final: prefijo + '=' (case-insensitive para robustez)
    upper = texto.upper()

    if not upper.startswith(REQUIRED_PREFIX):
        print("Código QR inválido: no inicia con ADITUMGATE=. Acceso denegado.")
        denied()
        return

    # Extraer dato tras el '='
    aditumData = texto[len(REQUIRED_PREFIX):]

    loading()
    if doorType == "exit":
        print("Procesando salida...")
        r = requests.get(f'https://caseta.aditumcr.com/api/aditum-gate-verifier-exit-secure/{aditumData}/{doorId}')
        print(f"Código de estado: {r.status_code}\nRespuesta: {r.text}")
    elif doorType == "entry":
        print("Procesando entrada...")
        r = requests.get(f'https://caseta.aditumcr.com/api/aditum-gate-verifier-entry-secure/{aditumData}/{doorId}')
        print(f"Código de estado: {r.status_code}\nRespuesta: {r.text}")
    else:
        print("No autorizado")
        denied()

# =========================
# Bucle principal
# =========================
if __name__ == "__main__":
    while True:
        result = keylogger()
        if result:
            procesar_texto(result)
