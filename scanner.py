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

ap = argparse.ArgumentParser()
ap.add_argument('-o', '--output', type=str, default='barcodes.csv',
                help='path to output CSV file containing barcodes')
args = vars(ap.parse_args())

# Cambia estos valores a los ID correctos de las puertas en la base de datos
entryDoorId = '0'
exitDoorId = '0'
placeName = 'PLACE'
showCameraFeed = False

if exitDoorId != '0':
    vsExit = VideoStream(src=0).start()

if entryDoorId != '0':
    vsEntry = VideoStream(src=2).start()

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
    
while True:
    if entryDoorId != '0':
        frameEntry = vsEntry.read()
        if frameEntry is None:
            print("The entry cam got disconnected")
            camInfo = f'de Entrada en {placeName}'
            time.sleep(100)
            requests.get(f'https://app.aditumcr.com/api/aditum-gate-cam-disconected/{camInfo}')
            subprocess.call("sudo reboot", shell=True)
        else:
            frameEntry = imutils.resize(frameEntry, width=400)
            barcodesEntry = pyzbar.decode(frameEntry)
            if showCameraFeed:
                cv2.imshow('Aditum QR Entry', frameEntry)
            for barcode in barcodesEntry:
                (x, y, w, h) = barcode.rect
                cv2.rectangle(frameEntry, (x, y), (x + w, y + h), (0, 0, 0xFF), 2)
                barcodeData = barcode.data.decode('utf-8')
                barcodeType = barcode.type
                fullQrText = '{}'.format(barcodeData)
                print(fullQrText)
                fullQrTextArray = fullQrText.split("=")
                if len(fullQrTextArray) == 2:
                    aditumData = fullQrTextArray[1]
                    aditumQrVerifying = fullQrTextArray[0]
                    cv2.putText(
                        frameEntry,
                        aditumData,
                        (x, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 0, 0xFF),
                        2,
                    )

                    if barcodeData != found:
                        if aditumQrVerifying == "ADITUMGATE":
                            if "EXIT" in fullQrText:
                                print("es de salida")
                            else:
                                # Realiza la solicitud POST al endpoint de validación
                                validation_response = send_request(f'aditum-gate-verifier-entry/{aditumData}/{entryDoorId}', {})
                                
                                if validation_response.get('isAutorized') == 'true':
                                    print("Autorizado")
                                    # Envía la solicitud POST al servidor Node.js
                                    nodejs_response = send_to_nodejs('api/code-accepted', {
                                        'name': validation_response.get('name'),
                                        'isAutorized': validation_response.get('isAutorized'),
                                        'isAutomatic': validation_response.get('isAutomatic')
                                    })
                                    print("Estado enviado a Node.js:", nodejs_response)
                                    
                                    # Guarda el código de barras en el CSV
                                    csv.write('{},{}\n'.format(datetime.datetime.now(), barcodeData))
                                    csv.flush()
                                    found = barcodeData
                                else:
                                    print("No autorizado")
                                    # Envía la solicitud POST al endpoint de denegación sin parámetros
                                    denial_response = send_to_nodejs('api/code-denied')
                                    print("Estado de denegación enviado a Node.js:", denial_response)
                    #cv2.imshow('Aditum QR Reader', frameEntry)
    if exitDoorId != '0':
        frameExit = vsExit.read()
        if frameExit is None:
            print("The exit cam got disconnected")
            camInfo = f'de Salida en {placeName}'
            time.sleep(100)
            requests.get(f'https://app.aditumcr.com/api/aditum-gate-cam-disconected/{camInfo}')
            subprocess.call("sudo reboot", shell=True)
        else:
            frameExit = imutils.resize(frameExit, width=400)
            barcodesExit = pyzbar.decode(frameExit)
            if showCameraFeed:
                cv2.imshow('Aditum QR EXIT', frameExit)
            for barcode in barcodesExit:
                (x, y, w, h) = barcode.rect
                cv2.rectangle(frameExit, (x, y), (x + w, y + h), (0, 0, 0xFF), 2)
                barcodeDataExit = barcode.data.decode('utf-8')
                barcodeType = barcode.type
                fullQrText = '{}'.format(barcodeDataExit)
                print(fullQrText)
                fullQrTextArray = fullQrText.split("=")
                if len(fullQrTextArray) == 2:
                    aditumData = fullQrTextArray[1]
                    aditumQrVerifying = fullQrTextArray[0]
                    cv2.putText(
                        frameExit,
                        aditumData,
                        (x, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 0, 0xFF),
                        2,
                    )

                if barcodeDataExit != found:
                    if aditumQrVerifying == "ADITUMGATE":
                        if "EXIT" in fullQrText:
                            r = requests.get(f'https://app.aditumcr.com/api/aditum-gate-verifier-exit/{aditumData}/{exitDoorId}')  
                            print("encontrado")
                            csv.write('{},{}\n'.format(datetime.datetime.now(), barcodeDataExit))
                            csv.flush()
                            found = barcodeDataExit 
                        else:
                            print("es de entrada")
                            # Aquí se envía la solicitud POST al endpoint de denegación sin parámetros
                            denial_response = send_to_nodejs('api/code-denied')
                            print("Estado de denegación enviado a Node.js:", denial_response)
                    #cv2.imshow('Aditum QR Reader', frameExit)
    key = cv2.waitKey(1) & 0xFF
    # if the `s` key is pressed, break from the loop
    if key == ord('s'):
        break

csv.close()
cv2.destroyAllWindows()