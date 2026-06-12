"""Lector QR por camara USB (OpenCV + pyzbar).

Reemplaza scanner.py y scannerExit.py: el indice de camara viene de la
config (scanners[].cameraIndex) en vez de ser la diferencia entre dos
archivos. Mantiene la recuperacion por uhubctl cuando la camara se cae.

showCameraFeed abre la ventana de cv2 para debug; bajo PM2 (sin X) debe
quedar en false.
"""
import logging
import subprocess
import time

import cv2
from pyzbar import pyzbar

from .base import Scanner

log = logging.getLogger("aditum.scanner.opencv")

FRAME_WIDTH = 480
FRAME_HEIGHT = 360
FPS = 60
# Reinicios de camara consecutivos antes de rendirse y dejar que PM2
# reinicie el proceso completo
MAX_CONSECUTIVE_RESTARTS = 5


class OpenCvScanner(Scanner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._capture = None
        self._failed_restarts = 0

    def _open_camera(self):
        capture = cv2.VideoCapture(self.reader.camera_index, cv2.CAP_V4L2)
        capture.set(cv2.CAP_PROP_FPS, FPS)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        return capture

    def _restart_camera(self):
        """Reinicia el bus USB para recuperar una camara colgada."""
        try:
            subprocess.call("sudo uhubctl -l 1-1 -a off", shell=True)
            time.sleep(2)
            subprocess.call("sudo uhubctl -l 1-1 -a on", shell=True)
            time.sleep(4)
            log.info("Bus USB de la camara %s reiniciado", self.reader.camera_index)
        except Exception as e:
            log.error("Error reiniciando la camara %s: %s", self.reader.camera_index, e)

    def read_code(self):
        if self._capture is None:
            self._capture = self._open_camera()

        while True:
            ret, frame = self._capture.read()
            if not ret or frame is None:
                log.warning("Camara %s sin señal; reiniciando bus USB",
                            self.reader.camera_index)
                self._capture.release()
                self._capture = None
                self._restart_camera()
                self._failed_restarts += 1
                if self._failed_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    log.error("Camara %s irrecuperable tras %s reinicios; "
                              "saliendo para que PM2 reinicie el proceso",
                              self.reader.camera_index, self._failed_restarts)
                    raise RuntimeError("camara irrecuperable")
                return None
            self._failed_restarts = 0

            barcodes = pyzbar.decode(frame)

            if self.reader.show_camera_feed:
                try:
                    cv2.imshow(f"Aditum QR {self.reader.role}", frame)
                    cv2.waitKey(1)
                except cv2.error:
                    # Sin servidor X (PM2/headless): deshabilitar el feed
                    self.reader.show_camera_feed = False

            for barcode in barcodes:
                data = barcode.data.decode("utf-8")
                log.debug("QR detectado en camara %s", self.reader.camera_index)
                return data
