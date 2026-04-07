#!/usr/bin/python
# -*- coding: utf-8 -*-

from flask import Flask, jsonify, request
import RPi.GPIO as GPIO
import time
import os
import requests
import board
import neopixel
import threading
from requests.auth import HTTPDigestAuth

isScreen = True
blinking = False

def set_color(color):
    pixels.fill(color)
    pixels.show()

def set_white():
    set_color((255, 255, 255))

def green_for_3_seconds():
    set_color((0, 255, 0))
    time.sleep(4)
    set_white()

def blink_yellow_forever():
    yellow = (255, 255, 0)
    global blinking
    while blinking:
        set_color(yellow)
        time.sleep(1)
        turn_off()
        time.sleep(1)

def start_blinking_yellow():
    global blinking
    blinking = True
    threading.Thread(target=blink_yellow_forever).start()

def stop_blinking_yellow():
    global blinking
    blinking = False

def red_for_3_seconds():
    set_color((255, 0, 0))
    time.sleep(4)
    set_white()

def turn_off():
    set_color((0, 0, 0))

app = Flask(__name__)

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

if isScreen:
    pixels = neopixel.NeoPixel(board.D18, 55, brightness=1, auto_write=False)
    set_white()
else:
    GPIO.setmode(GPIO.BOARD)
    for gate in gates:
        GPIO.setup(gate["pin"], GPIO.OUT)
        GPIO.output(gate["pin"], GPIO.HIGH)

# ============================================================
# Hikvision ISAPI - Registro dinamico de tarjetas QR
# ============================================================
active_employees = {}

def hikvision_delete_card(ip, user, password, employee_no):
    try:
        url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Delete?format=json"
        payload = {
            "CardInfoDelCond": {
                "EmployeeNoList": [{"employeeNo": employee_no}]
            }
        }
        resp = requests.put(url, json=payload, auth=HTTPDigestAuth(user, password), timeout=5)
        return resp.status_code
    except Exception as e:
        print(f"Error deleting card from {ip}: {e}")
        return None

def hikvision_ensure_user(ip, user, password, employee_no):
    try:
        search_url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Search?format=json"
        search_payload = {
            "UserInfoSearchCond": {
                "searchID": "1",
                "maxResults": 1,
                "searchResultPosition": 0,
                "EmployeeNoList": [{"employeeNo": employee_no}]
            }
        }
        resp = requests.post(search_url, json=search_payload, auth=HTTPDigestAuth(user, password), timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("UserInfoSearch", {}).get("totalMatches", 0) > 0:
                return 200

        url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Record?format=json"
        payload = {
            "UserInfo": {
                "employeeNo": employee_no,
                "name": "Bienvenido",
                "userType": "normal",
                "Valid": {
                    "enable": True,
                    "beginTime": "2024-01-01T00:00:00",
                    "endTime": "2037-12-31T23:59:59",
                    "timeType": "local"
                },
                "RightPlan": [{"doorNo": 1, "planTemplateNo": "1"}],
                "doorRight": "1",
                "localUIRight": False
            }
        }
        resp = requests.post(url, json=payload, auth=HTTPDigestAuth(user, password), timeout=5)
        return resp.status_code
    except Exception as e:
        print(f"Error ensuring user on {ip}: {e}")
        return None

def hikvision_register_card(ip, user, password, card_no, employee_no):
    try:
        url = f"http://{ip}/ISAPI/AccessControl/CardInfo/Record?format=json"
        payload = {
            "CardInfo": {
                "employeeNo": employee_no,
                "cardNo": card_no,
                "cardType": "normalCard"
            }
        }
        resp = requests.post(url, json=payload, auth=HTTPDigestAuth(user, password), timeout=5)
        return resp.status_code
    except Exception as e:
        print(f"Error registering card on {ip}: {e}")
        return None

def hikvision_delete_user(ip, user, password, employee_no):
    try:
        url = f"http://{ip}/ISAPI/AccessControl/UserInfo/Delete?format=json"
        payload = {
            "UserInfoDetail": {
                "mode": "byEmployeeNo",
                "EmployeeNoList": [{"employeeNo": employee_no}]
            }
        }
        resp = requests.put(url, json=payload, auth=HTTPDigestAuth(user, password), timeout=5)
        return resp.status_code
    except Exception as e:
        print(f"Error deleting user from {ip}: {e}")
        return None

def hikvision_update_card(ip, user, password, card_no, employee_no):
    hikvision_ensure_user(ip, user, password, employee_no)
    hikvision_delete_card(ip, user, password, employee_no)
    return hikvision_register_card(ip, user, password, card_no, employee_no)

@app.route('/update-card', methods=['POST'])
def update_card():
    data = request.get_json()
    if not data or 'cardNo' not in data or 'terminals' not in data:
        return jsonify({"error": "cardNo and terminals required"}), 400

    card_no = data['cardNo']
    employee_no = data.get('employeeNo', '99999')
    terminals = data['terminals']
    results = []

    for terminal in terminals:
        ip = terminal.get('ip')
        user = terminal.get('user', 'admin')
        password = terminal.get('password', '')

        if not ip:
            continue

        status = hikvision_update_card(ip, user, password, card_no, employee_no)
        results.append({"ip": ip, "status": status, "employeeNo": employee_no})

        if ip not in active_employees:
            active_employees[ip] = {}
        active_employees[ip][employee_no] = {"user": user, "password": password}

    return jsonify({"cardNo": card_no, "results": results})

@app.route('/cleanup-cards', methods=['POST'])
def cleanup_cards():
    results = []
    for ip, employees in active_employees.items():
        for employee_no, creds in employees.items():
            status = hikvision_delete_user(ip, creds["user"], creds["password"], employee_no)
            results.append({"ip": ip, "employeeNo": employee_no, "status": status})
    active_employees.clear()
    return jsonify({"results": results})

def nightly_cleanup():
    while True:
        now = time.localtime()
        target_hour = 2
        seconds_until = ((target_hour - now.tm_hour) % 24) * 3600 - now.tm_min * 60 - now.tm_sec
        if seconds_until <= 0:
            seconds_until += 86400
        time.sleep(seconds_until)
        for ip, employees in list(active_employees.items()):
            for employee_no, creds in list(employees.items()):
                hikvision_delete_user(ip, creds["user"], creds["password"], employee_no)
        active_employees.clear()

threading.Thread(target=nightly_cleanup, daemon=True).start()

# ============================================================

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static'))

@app.route('/', defaults={'file': 'index.html'})
def index():
    return app.send_directory(STATIC_DIR, 'index.html')

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
    time.sleep(1)
    GPIO.output(gate["pin"], GPIO.HIGH)
    return jsonify({"id": gate["id"], "status": gate["status"]})

@app.route('/code-accepted/<string:name>')
def code_accepted(name):
    validation_response = {
        'name': name,
        'isAutorized': True,
        'isAutomatic': True
    }
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
        'message': 'Access granted',
        'nodejs_response': nodejs_response
    })

@app.route('/wait-for-response/<string:name>')
def wait_for_response(name):
    validation_response = {
        'name': name,
        'isAutorized': True,
        'isAutomatic': True
    }
    nodejs_response = send_to_nodejs('api/wait-for-response', {
        'name': validation_response.get('name'),
        'isAutorized': validation_response.get('isAutorized'),
        'isAutomatic': validation_response.get('isAutomatic')
    })
    start_blinking_yellow()
    return jsonify({
        'message': 'Waiting for response',
        'nodejs_response': nodejs_response
    })

@app.route('/code-denied/<string:name>')
def code_denied(name):
    validation_response = {
        'name': name,
        'isAutorized': True,
        'isAutomatic': True
    }
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