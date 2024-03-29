#!/usr/bin/python
# -*- coding: utf-8 -*-

import requests

headers = {
    'Content-type': 'application/json',
}



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

#CHANGE TO THE ID OF THE GATE_DOOR IN DB ADITUM
entryDoorId = '0'
exitDoorId = '0'
placeName = 'PLACE'
showCameraFeed = False
#
if(exitDoorId!='0'):
    vsExit = VideoStream(src=0).start()
 
if(entryDoorId!='0'):
    vsEntry = VideoStream(src=2).start()  
#vs = VideoStream(usePiCamera=True).start()  # For Pi Camera

csv = open(args['output'], 'w')
found = ""

while True:
    if(entryDoorId!='0'):
        frameEntry = vsEntry.read()
        if frameEntry is None: 
            print("The entry cam got disconected");
            camInfo = 'de Entrada en '+placeName;
            time.sleep(100);
            r = requests.get('https://app.aditumcr.com/api/aditum-gate-cam-disconected/'+camInfo) 
            subprocess.call("sudo reboot",shell=True);
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
                print (fullQrText)
                fullQrTextArray = fullQrText.split("=")
                if len(fullQrTextArray)==2:
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

                    # if the barcode text is currently not in our CSV file, write
                    # the timestamp + barcode to disk and update the set
                    #cv2.imshow('Aditum QR Reader', frameEntry)
                    if barcodeData != found:
                        if(aditumQrVerifying=="ADITUMGATE"):
                            if "EXIT" in fullQrText:
                                print("es de salida")
                            else:
                              r = requests.get('https://app.aditumcr.com/api/aditum-gate-verifier-entry/'+aditumData+'/'+entryDoorId)  
                              print("encontrado")
                              csv.write('{},{}\n'.format(datetime.datetime.now(),barcodeData))
                              csv.flush()
                              found = barcodeData
                    #cv2.imshow('Aditum QR Reader', frameEntry)
    if(exitDoorId!='0'):
        frameExit = vsExit.read()
        if frameExit is None: 
            print("The exit cam got disconected");
            camInfo = 'de Salida en '+placeName;
            time.sleep(100);
            r = requests.get('https://app.aditumcr.com/api/aditum-gate-cam-disconected/'+camInfo) 
            subprocess.call("sudo reboot",shell=True);
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
                print (fullQrText)
                fullQrTextArray = fullQrText.split("=")
                if len(fullQrTextArray)==2:
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

                    # if the barcode text is currently not in our CSV file, write
                    # the timestamp + barcode to disk and update the set

                    if barcodeDataExit != found:
                        if(aditumQrVerifying=="ADITUMGATE"):
                            if "EXIT" in fullQrText:
                              r = requests.get('https://app.aditumcr.com/api/aditum-gate-verifier-exit/'+aditumData+'/'+exitDoorId)  
                              print("encontrado")
                              csv.write('{},{}\n'.format(datetime.datetime.now(),barcodeDataExit))
                              csv.flush()
                              found = barcodeDataExit 
                            else:
                                print("es de entrada")
                    #cv2.imshow('Aditum QR Reader', frameExit)
    key = cv2.waitKey(1) & 0xFF
    # if the `s` key is pressed, break from the loop

    if key == ord('s'):
        break
csv.close()
cv2.destroyAllWindows()



