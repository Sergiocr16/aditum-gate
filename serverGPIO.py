#!/usr/bin/python
# -*- coding: utf-8 -*-

from flask import Flask, jsonify
import RPi.GPIO as GPIO
import time
import os
import requests
import board
import neopixel
import threading

isScreen = True
blinking = False
# Initialize the NeoPixel strip
# GPIO Data Pin: D18, Number of LEDs: 55, Brightness: 1
# Function to set all LEDs to a specific color
def set_color(color):
    pixels.fill(color)
    pixels.show()

# Function to set all LEDs to white
def set_white():
    set_color((255, 255, 255))

# Function to turn LEDs green for 3 seconds
def green_for_3_seconds():
    set_color((0, 255, 0))
    time.sleep(4)
    set_white()
    
import threading
import time

blinking = False  # Variable de control para el parpadeo

# Función para parpadeo amarillo continuo
def blink_yellow_forever():
    yellow = (255, 255, 0)  # Color amarillo (rojo + verde)
    
    global blinking
    while blinking:
        set_color(yellow)  # Encender en amarillo
        time.sleep(1)
        turn_off()  # Apagar (blanco)
        time.sleep(1)

# Función para iniciar el parpadeo
def start_blinking_yellow():
    global blinking
    blinking = True
    # Ejecutar el parpadeo en un hilo separado
    threading.Thread(target=blink_yellow_forever).start()

# Función para detener el parpadeo
def stop_blinking_yellow():
    global blinking
    blinking = False  # Esto hará que el bucle termine

# Function to turn LEDs red for 3 seconds
def red_for_3_seconds():
    set_color((255, 0, 0))
    time.sleep(4)
    set_white()

    # Function to turn off all LEDs
def turn_off():
    set_color((0, 0, 0))

app = Flask(__name__)
if isScreen:
    pixels = neopixel.NeoPixel(board.D18, 55, brightness=1, auto_write=False)
    set_white()
else:
    GPIO.setmode(GPIO.BOARD)
    for gate in gates:
        GPIO.setup(gate["pin"], GPIO.OUT)
        GPIO.output(gate["pin"], GPIO.HIGH)  # Inicialmente apagar el pin

# Configuración de los pines GPIO

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


# Ruta al directorio donde se encuentra el archivo index.html
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static'))

@app.route('/',defaults={'file': 'index.html'})
def index():
    return app.send_directory(STATIC_DIR,'index.html')

def send_to_nodejs(endpoint, data=None):
    url = f'http://localhost:3000/{endpoint}'
    response = requests.post(url, json=data)
    return response.json()

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

@app.route('/code-accepted/<string:name>')
def code_accepted(name):
    # Simulación de una validación o lógica usando el 'code'
    validation_response = {
        'name': name,  # Aquí 'code' es el valor que recibimos en la URL
        'isAutorized': True,  # Puedes cambiar esta lógica según necesites
        'isAutomatic': True
    }
    
    # Llamada a Node.js, pasando 'name' y otras propiedades de la validación
    nodejs_response = send_to_nodejs('api/code-accepted', {
        'name': validation_response.get('name'),
        'isAutorized': validation_response.get('isAutorized'),
        'isAutomatic': validation_response.get('isAutomatic')
    })
    if blinking:
        stop_blinking_yellow()
        time.sleep(0.5)
        green_for_3_seconds()
    else:
        green_for_3_seconds()    
    return jsonify({
        'message': 'Access denied',
        'nodejs_response': nodejs_response
    }) 
 
@app.route('/wait-for-response/<string:name>')
def wait_for_response(name):
    # Simulación de una validación o lógica usando el 'code'
    validation_response = {
        'name': name,  # Aquí 'code' es el valor que recibimos en la URL
        'isAutorized': True,  # Puedes cambiar esta lógica según necesites
        'isAutomatic': True
    }
    
    # Llamada a Node.js, pasando 'name' y otras propiedades de la validación
    nodejs_response = send_to_nodejs('api/wait-for-response', {
        'name': validation_response.get('name'),
        'isAutorized': validation_response.get('isAutorized'),
        'isAutomatic': validation_response.get('isAutomatic')
    })
    # Ejecutar el parpadeo en un hilo separado
    start_blinking_yellow()
    return jsonify({
        'message': 'Access denied',
        'nodejs_response': nodejs_response
    }) 
    
@app.route('/code-denied/<string:name>')
def code_denied(name):
    # Simulación de una validación o lógica usando el 'code'
    validation_response = {
        'name': name,  # Aquí 'code' es el valor que recibimos en la URL
        'isAutorized': True,  # Puedes cambiar esta lógica según necesites
        'isAutomatic': True
    }
    
    # Llamada a Node.js, pasando 'name' y otras propiedades de la validación
    nodejs_response = send_to_nodejs('api/code-denied', {
        'name': validation_response.get('name'),
        'isAutorized': validation_response.get('isAutorized'),
        'isAutomatic': validation_response.get('isAutomatic')
    })
    if blinking:
        stop_blinking_yellow()
        time.sleep(0.5)
        red_for_3_seconds()
    else:
        red_for_3_seconds()    
    return jsonify({
        'message': 'Access denied',
        'nodejs_response': nodejs_response
    })

@app.route('/closeGate/<int:id>')
def close_gate(id):
    gate = gates[id - 1]
    gate["status"] = 0
    GPIO.output(gate["pin"], GPIO.HIGH)
    return jsonify({"id": gate["id"], "status": gate["status"]})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
