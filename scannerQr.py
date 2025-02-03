import evdev
from evdev import ecodes, categorize
import requests
import time

# Variables
doorType = "entry"  # Set this to "exit" or "entry"
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
    if hasScreen:  # Evitar enviar solicitudes si no hay pantalla
        url = f'http://localhost:3000/{endpoint}'
        response = requests.post(url, json=data)
        return response.json()

def loading():
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
        # Scancode: ASCIICode (sin shift)
        0: None, 1: 'ESC', 2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 
        10: '9', 11: '0', 12: '-', 13: '=', 14: 'BKSP', 15: 'TAB', 16: 'q', 17: 'w', 18: 'e', 19: 'r', 
        20: 't', 21: 'y', 22: 'u', 23: 'i', 24: 'o', 25: 'p', 26: '[', 27: ']', 28: 'CRLF', 29: 'LCTRL', 
        30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h', 36: 'j', 37: 'k', 38: 'l', 39: ';', 
        40: '"', 41: '`', 42: 'LSHFT', 43: '\\', 44: 'z', 45: 'x', 46: 'c', 47: 'v', 48: 'b', 49: 'n', 
        50: 'm', 51: ',', 52: '.', 53: '/', 54: 'RSHFT', 56: 'LALT', 57: ' ', 100: 'RALT', 
        79: '1', 80: '2', 81: '3', 75: '4', 76: '5', 
        77: '6', 71: '7', 72: '8', 73: '9', 82: '0'
    } 

    caps_codes = { 
        # Scancode: Caracter con Shift (o mayúsculas/símbolos)
        0: None, 1: 'ESC', 2: '!', 3: '@', 4: '#', 5: '$', 6: '%', 7: '^', 8: '&', 9: '*', 
        10: '(', 11: ')', 12: '_', 13: '+', 14: 'BKSP', 15: 'TAB', 16: 'Q', 17: 'W', 18: 'E', 19: 'R', 
        20: 'T', 21: 'Y', 22: 'U', 23: 'I', 24: 'O', 25: 'P', 26: '{', 27: '}', 28: 'CRLF', 29: 'LCTRL', 
        30: 'A', 31: 'S', 32: 'D', 33: 'F', 34: 'G', 35: 'H', 36: 'J', 37: 'K', 38: 'L', 39: ':', 
        40: u"'", 41: u'~', 42: u'LSHFT', 43: u'|', 44: u'Z', 45: u'X', 46: u'C', 47: u'V', 48: u'B', 49: u'N', 
        50: u'M', 51: u'<', 52: u'>', 53: u'?', 54: u'RSHFT', 56: u'LALT', 57: u' ', 100: u'RALT'
    } 

    caps = False           # Estado de la tecla Shift
    current_text = ''      # Acumula el texto ingresado

    print("Escaneando QR... (Esperando código terminado en '@')")

    for event in dev.read_loop(): 
        if event.type == ecodes.EV_KEY: 
            data = categorize(event)
            
            # Detecta si se presiona o libera Shift
            if data.scancode == 42:
                if data.keystate == 1:  # Tecla presionada
                    caps = True 
                elif data.keystate == 0:  # Tecla liberada
                    caps = False 
            
            # Procesa solo teclas presionadas
            if data.keystate == 1:
                if caps:
                    key_lookup = caps_codes.get(data.scancode, '')
                else:
                    key_lookup = scancodes.get(data.scancode, '')

                # Si es un carácter válido, lo añadimos al texto
                if key_lookup not in (None, 'LSHFT', 'RSHFT', 'CRLF'):
                    current_text += key_lookup
                    print(key_lookup, end='', flush=True)

                # Si detectamos el '@' al final, devolvemos el código escaneado
                if key_lookup == '@':
                    print("\nQR Detectado, procesando...")
                    return current_text

def keylogger(device_path='/dev/input/event0'):
    return read_events(device_path)

# Función para procesar el código QR leído
def procesar_texto(texto):
    # Eliminamos el '@' final para procesarlo correctamente
    texto = texto.rstrip('@')

    # Separamos el texto en base a '='
    fullQrTextArray = texto.split("=")
    
    # Verificamos si el código es válido
    if len(fullQrTextArray) == 2:
        aditumQrVerifying = fullQrTextArray[0]
        aditumData = fullQrTextArray[1]

        # Si el código empieza con "ADITUMGATE", se verifica
        if aditumQrVerifying == "ADITUMGATE":
            if doorType == "exit":
                if "EXIT" in fullQrTextArray:
                    loading()
                    print("Processing exit...")
                    r = requests.get('https://app.aditumcr.com/api/aditum-gate-verifier-exit/' + aditumData + '/' + doorId)
                    time.sleep(3)
                    found = fullQrTextArray
                else:
                    loading()
                    print("Processing exit (no EXIT in QR)...")
                    r = requests.get('https://app.aditumcr.com/api/aditum-gate-verifier-exit/' + aditumData + 'ENTRY/' + doorId)
                    time.sleep(3)
                    found = fullQrTextArray
            elif doorType == "entry":
                try:
                    loading()
                    print("Processing entry F...")
                    print("URL:", f'https://app.aditumcr.com/api/aditum-gate-verifier-entry/{aditumData}/{doorId}')
                    r = requests.get(f'https://app.aditumcr.com/api/aditum-gate-verifier-entry/{aditumData}/{doorId}')
                    print("Response Status Code:", r.status_code)
                    print("Response Content:", r.text)
                except requests.exceptions.RequestException as e:
                    print("Error en la solicitud:", str)
            else:
                print("No autorizado")
                denied()

# Bucle principal: espera un código válido y lo procesa cuando termina en '@'
while True:
    result = keylogger()
    if result:
        procesar_texto(result)

