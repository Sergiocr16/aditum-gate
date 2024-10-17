#!/usr/bin/python
# -*- coding: utf-8 -*-
import requests
import json
from imutils.video import VideoStream
from pyzbar import pyzbar
import argparse
import datetime
import imutils
import time
import cv2
import subprocess


# Argument parser for output file
ap = argparse.ArgumentParser()
ap.add_argument('-o', '--output', type=str, default='barcodes.csv',
                help='path to output CSV file containing barcodes')
args = vars(ap.parse_args())

# Variables
doorType = "entry"  # Set this to "exit" or "entry"
doorId = '0'  # Assign the correct ID based on the type of door
placeName = 'PLACE'
showCameraFeed = False
frame_counter = 0
process_every_n_frames = 1

# Initialize the camera
vs = VideoStream(src=0).start()
csv = open(args['output'], 'w')
found = ""

def send_request(endpoint, data):
    url = f'https://app.aditumcr.com/api/{endpoint}'
    response = requests.post(url, json=data)
    return response.json()

def send_to_nodejs(endpoint, data=None):
    url = f'http://localhost:3000/{endpoint}'
    response = requests.post(url, json=data)
    return response.json()

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

while True:
    # Read frame from camera
    frame_counter += 1
    frame = vs.read()
    if frame_counter % process_every_n_frames != 0:
        continue
    if frame is None:
        print("Intentando reconectar la cámara...")
        vs.stop()
        time.sleep(1)  # Espera un tiempo antes de intentar reconectar
        restart_camera();
        vs = VideoStream(src=0).start()
       
    else:
        frame = imutils.resize(frame, width=400)
        barcodes = pyzbar.decode(frame)

        if showCameraFeed:
            cv2.imshow(f'Aditum QR {doorType.capitalize()}', frame)

        for barcode in barcodes:
            (x, y, w, h) = barcode.rect
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 0, 0xFF), 2)
            barcodeData = barcode.data.decode('utf-8')
            barcodeType = barcode.type
            fullQrText = '{}'.format(barcodeData)
            print(fullQrText)
            fullQrTextArray = fullQrText.split("=")
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
                        if "EXIT" in fullQrText and doorType == "exit":
                            print("Processing exit...")
                            r = requests.get(f'https://app.aditumcr.com/api/aditum-gate-verifier-exit/{aditumData}/{doorId}')
                            csv.write('{},{}\n'.format(datetime.datetime.now(), barcodeData))
                            csv.flush()
                            found = barcodeData
                        elif doorType == "entry":
                            print("Processing entry...")
                            validation_response = send_request(f'aditum-gate-verifier-entry/{aditumData}/{doorId}', {})
                            if validation_response.get('isAutorized') == 'true':
                                print("Autorizado")
                                nodejs_response = send_to_nodejs('api/code-accepted', {
                                    'name': validation_response.get('name'),
                                    'isAutorized': validation_response.get('isAutorized'),
                                    'isAutomatic': validation_response.get('isAutomatic')
                                })
                                print("Estado enviado a Node.js:", nodejs_response)
                                csv.write('{},{}\n'.format(datetime.datetime.now(), barcodeData))
                                csv.flush()
                                found = barcodeData
                            else:
                                print("No autorizado")
                                denial_response = send_to_nodejs('api/code-denied')
                                print("Estado de denegación enviado a Node.js:", denial_response)

    # Exit the loop if 's' is pressed
    key = cv2.waitKey(1) & 0xFF
    if key == ord('s'):
        break
# Cleanup
csv.close()
cv2.destroyAllWindows()
