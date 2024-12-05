# -*- coding: utf-8 -*-
import requests
import json
from pyzbar import pyzbar
import argparse
import time
import cv2
import subprocess

# Argument parser for output file
ap = argparse.ArgumentParser()
ap.add_argument('-o', '--output', type=str, default='barcodes.csv',
                help='path to output CSV file containing barcodes')
args = vars(ap.parse_args())

# Variables
doorType = "exit"  # Set this to "exit" or "entry"
doorId = '250'  # Assign the correct ID based on the type of door
placeName = 'Entrada Name'
showCameraFeed = True
frame_counter = 0
process_every_n_frames = 1
last_barcode_text = "Esperando QR..."  # Variable para almacenar el último texto leído

# Initialize the camera
vs = cv2.VideoCapture(2, cv2.CAP_V4L2)

vs.set(cv2.CAP_PROP_FPS, 60)  # Configurar FPS de la cámara
vs.set(cv2.CAP_PROP_FRAME_WIDTH, 480)  # Ancho de video
vs.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)  # Altura de video

found = ""

# Funciones
def send_request(endpoint, data):
    url = f'https://caseta.aditumcr.com/api/{endpoint}'
    response = requests.post(url, json=data)
    return response.json()

def send_to_nodejs(endpoint, data=None):
    url = f'http://localhost:3000/{endpoint}'
    response = requests.post(url, json=data)
    return response.json()

def loading():
    url = f'http://localhost:3000/api/loading'
    response = requests.post(url, json={"name": "loading"})
    return response

def denied():
    return "a"

def restart_camera():
    try:
        # Reinicia el bus USB para reconectar la cámara
        subprocess.call("sudo uhubctl -l 1-1 -a off", shell=True)
        time.sleep(2)
        subprocess.call("sudo uhubctl -l 1-1 -a on", shell=True)
        time.sleep(4)
        print("Cámara reiniciada.")
    except Exception as e:
        print(f"Error reiniciando la cámara: {str(e)}")

# Bucle principal
while True:
    frame_counter += 1
    ret, frame = vs.read()

    # Verificar si la cámara está funcionando correctamente
    if not ret or frame is None:
        print("Error al leer el frame de la cámara. Intentando reconectar...")
        vs.release()
        restart_camera()
        vs = cv2.VideoCapture(0)
        vs.set(cv2.CAP_PROP_FPS, 60)
        vs.set(cv2.CAP_PROP_FRAME_WIDTH, 300)
        vs.set(cv2.CAP_PROP_FRAME_HEIGHT, 300)
        continue

    # Procesar cada n frames
    if frame_counter % process_every_n_frames != 0:
        continue

    barcodes = pyzbar.decode(frame)

    # Mostrar la cámara si está habilitado
    if showCameraFeed:
        # Agregar el texto del último código QR leído en la parte superior izquierda
        cv2.putText(
            frame, 
            f"QR: {last_barcode_text}", 
            (10, 30), 
            cv2.FONT_HERSHEY_SIMPLEX, 
            1, 
            (255, 0, 0), 
            2
        )
        cv2.imshow(f'Aditum QR {doorType.capitalize()}', frame)

    # Procesar los códigos QR detectados
    for barcode in barcodes:
        (x, y, w, h) = barcode.rect
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 0, 0xFF), 2)
        barcodeData = barcode.data.decode('utf-8')
        barcodeType = barcode.type
        last_barcode_text = barcodeData  # Actualizar el texto mostrado en la pantalla
        print(f"QR detectado: {last_barcode_text}")

        fullQrTextArray = barcodeData.split("=")
        if len(fullQrTextArray) == 2:
            aditumData = fullQrTextArray[1]
            aditumQrVerifying = fullQrTextArray[0]
            cv2.putText(
                frame,
                aditumData,
                (x, y - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 0, 0xFF),
                2,
            )
            if barcodeData != found:
                if aditumQrVerifying == "ADITUMGATE":
                    if "EXIT" in barcodeData and doorType == "exit":
                        loading()
                        print("Processing exit...")
                        r = requests.get('https://caseta.aditumcr.com/api/aditum-gate-verifier-exit/' + aditumData + '/' + doorId)
                        time.sleep(1)
                        found = barcodeData
                    elif "EXIT" not in barcodeData and doorType == "entry":
                        loading()
                        print("Processing entry...")
                        r = requests.get('https://caseta.aditumcr.com/api/aditum-gate-verifier-entry/' + aditumData + '/' + doorId)
                        time.sleep(1)
                        found = barcodeData
                    else:
                        print("No autorizado")
                        denied()

    # Salir si se presiona la tecla 's'
    key = cv2.waitKey(1) & 0xFF
    if key == ord('s'):
        break

# Liberar recursos
vs.release()
cv2.destroyAllWindows()
