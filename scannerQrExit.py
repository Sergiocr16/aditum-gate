import evdev
from evdev import ecodes, categorize
import requests
import time

# Variables
doorType = "exit"  # Set this to "exit" or "entry"
doorId = '0'  # Assign the correct ID based on the type of door
placeName = 'Name'
hasScreen = True
found = ""

# Funciones
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

def denied():
    return "Acceso denegado"

def read_events(device_path):
    dev = evdev.InputDevice(device_path)
    dev.grab()  # Toma el control exclusivo del dispositivo

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

    caps = False
    current_text = ''

    print("Escaneando QR... (Presione Enter para procesar el código)")

    for event in dev.read_loop():
        if event.type == ecodes.EV_KEY:
            data = categorize(event)

            if data.scancode == 42:
                caps = data.keystate == 1

            if data.keystate == 1:
                key_lookup = caps_codes.get(data.scancode) if caps else scancodes.get(data.scancode, '')

                if key_lookup not in (None, 'LSHFT', 'RSHFT', 'CR'):
                    current_text += key_lookup
                    print(key_lookup, end='', flush=True)

                if key_lookup == 'CR':
                    print("\nCódigo detectado, procesando...")
                    return current_text

def keylogger(device_path='/dev/input/event0'):
    return read_events(device_path)

def procesar_texto(texto):
    texto = texto.strip()
    fullQrTextArray = texto.split("=")

    if len(fullQrTextArray) == 2:
        aditumQrVerifying, aditumData = fullQrTextArray

        if aditumQrVerifying == "ADITUMGATE":
            loading()
            if doorType == "exit":
                print("Procesando salida...")
                r = requests.get(f'https://app.aditumcr.com/api/aditum-gate-verifier-exit/{aditumData}/{doorId}')
                print(f"Código de estado: {r.status_code}\nRespuesta: {r.text}")
            elif doorType == "entry":
                print("Procesando entrada...")
                r = requests.get(f'https://app.aditumcr.com/api/aditum-gate-verifier-entry/{aditumData}/{doorId}')
                print(f"Código de estado: {r.status_code}\nRespuesta: {r.text}")
            else:
                print("No autorizado")
                denied()
        else:
            print("Código QR inválido")

# Bucle principal
while True:
    result = keylogger()
    if result:
        procesar_texto(result)
