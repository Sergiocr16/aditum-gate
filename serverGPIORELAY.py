#!/usr/bin/python
# -*- coding: utf-8 -*-

from flask import Flask, jsonify
import RPi.GPIO as GPIO
import time
import os
import subprocess

app = Flask(__name__)

# Configuración de los pines GPIO
GPIO.setmode(GPIO.BOARD)
gates = [
    {"id": 1, "status": 0, "pin": 7},
    {"id": 2, "status": 0, "pin": 11},
    {"id": 3, "status": 0, "pin": 12},
    {"id": 4, "status": 0, "pin": 13},
    {"id": 5, "status": 0, "pin": 15},
    {"id": 6, "status": 0, "pin": 16},
    {"id": 7, "status": 0, "pin": 18},
    {"id": 8, "status": 0, "pin": 22},
    {"id": 9, "status": 0, "pin": 29},
    {"id": 10, "status": 0, "pin": 35},
]

for gate in gates:
    GPIO.setup(gate["pin"], GPIO.OUT)
    GPIO.output(gate["pin"], GPIO.HIGH)  # Inicialmente apagar el pin

# Ruta al directorio donde se encuentra el archivo index.html
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static'))

@app.route('/',defaults={'file': 'index.html'})
def index():
    return app.send_directory(STATIC_DIR,'index.html')

@app.route('/restart', methods=['GET'])
def restart():
    try:
        subprocess.Popen(['sudo', 'reboot'])
        return jsonify({"status": "restarting"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/gateStatus')
def gate_status():
    return jsonify(gates)

@app.route('/gateStatus/<int:id>')
def gate_status_id(id):
    gate = gates[id - 1]
    value = GPIO.input(gate["pin"])
    return jsonify({"value": value})

@app.route('/openGate/<int:id>')
def open_gate(id):
    gate = gates[id - 1]
    gate["status"] = 1
    GPIO.setup(gate["pin"], GPIO.OUT)
    GPIO.output(gate["pin"], GPIO.LOW)
    time.sleep(1)  # Espera 1 segundo
    GPIO.output(gate["pin"], GPIO.HIGH)
    return jsonify({"id": gate["id"], "status": gate["status"]})

@app.route('/closeGate/<int:id>')
def close_gate(id):
    gate = gates[id - 1]
    gate["status"] = 0
    GPIO.output(gate["pin"], GPIO.HIGH)
    return jsonify({"id": gate["id"], "status": gate["status"]})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
