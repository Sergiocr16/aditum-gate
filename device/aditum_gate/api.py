"""API Flask del dispositivo (puerto 8080).

Es el "Entry Point" que el backend de Aditum tiene configurado por punto de
acceso. TODOS los endpoints exigen el token del dispositivo (ver auth.py);
solo GET / es publico y minimo. El contrato completo esta en docs/API.md.
"""
import logging
import os
import tempfile
import threading

from flask import Flask, jsonify, request, send_from_directory

from .auth import init_auth
from .config_agent import SUPPORTED_SCHEMA_VERSION, apply_config, restart_process
from .settings import DEVICE_TOKEN_FILE

log = logging.getLogger("aditum.api")

RESTART_RESPONSE_GRACE = 1.0  # segundos para que la respuesta HTTP salga antes del exit

TOKEN_MIN_LEN = 16
TOKEN_MAX_LEN = 256


def _write_token_file(token):
    fd, tmp_path = tempfile.mkstemp(
        dir=str(DEVICE_TOKEN_FILE.parent), prefix=".device-token.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        os.replace(tmp_path, DEVICE_TOKEN_FILE)
        os.chmod(DEVICE_TOKEN_FILE, 0o600)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def create_app(settings, gates, hikvision_service, screen):
    app = Flask(__name__)
    init_auth(app, settings)

    # ------------------------------------------------------------
    # Publico: health minimo, sin informacion del dispositivo
    # ------------------------------------------------------------
    @app.route("/")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/admin")
    def admin_page():
        # Editor local de configuracion (http://localhost:8080/admin).
        # El HTML es publico; los datos (GET/PUT /config) exigen token.
        return send_from_directory(
            os.path.join(os.path.dirname(__file__), "static"), "admin.html"
        )

    # ------------------------------------------------------------
    # Estado y configuracion (administracion desde Aditum)
    # ------------------------------------------------------------
    @app.route("/status")
    def status():
        return jsonify({
            "deviceId": settings.device_id,
            "placeName": settings.place_name,
            "scannerType": settings.scanner_type,
            "configRevision": settings.config_revision,
            "schemaVersion": SUPPORTED_SCHEMA_VERSION,
            "provisioned": bool(settings.device_token),
            "configSource": settings.source.name if settings.source else None,
            "hasScreen": settings.has_screen,
            "gates": [g["id"] for g in gates.status_all()],
            "hikvisionEnabled": settings.hikvision_enabled,
            "pollingEnabled": settings.polling_enabled,
        })

    @app.route("/config")
    def get_config():
        return jsonify({
            "config": settings.raw,
            "source": settings.source.name if settings.source else None,
        })

    @app.route("/config", methods=["PUT"])
    def put_config():
        new_config = request.get_json(silent=True)
        if not isinstance(new_config, dict):
            return jsonify({"error": "body must be a JSON object"}), 400

        # Defensa contra entry points intercambiados: la config de otra Pi
        # no se aplica. deviceId vacio/ausente = config generica, se acepta.
        body_device_id = new_config.get("deviceId") or ""
        if body_device_id and settings.device_id and body_device_id != settings.device_id:
            return jsonify({
                "error": "deviceId mismatch",
                "expected": settings.device_id,
            }), 409

        result = apply_config(new_config, settings)
        if result.get("error"):
            http_status = 400 if result["error"] == "invalid config" else 409
            return jsonify(result), http_status

        if result["willRestart"]:
            # La respuesta debe salir antes del exit (mismo patron que /restart)
            threading.Timer(RESTART_RESPONSE_GRACE, restart_process).start()
        return jsonify(result)

    @app.route("/token", methods=["PUT"])
    def put_token():
        data = request.get_json(silent=True) or {}
        token = data.get("token", "")
        if (not isinstance(token, str) or not token.strip() or token != token.strip()
                or any(c.isspace() for c in token)
                or not TOKEN_MIN_LEN <= len(token) <= TOKEN_MAX_LEN):
            return jsonify({
                "error": "invalid token",
                "details": [f"token: {TOKEN_MIN_LEN}-{TOKEN_MAX_LEN} caracteres, sin espacios"],
            }), 400

        first_provision = not settings.device_token
        _write_token_file(token)
        settings.device_token = token  # efecto inmediato (auth lo lee por request)
        log.warning("Token de dispositivo %s", "provisionado" if first_provision else "rotado")
        return jsonify({"provisioned": True} if first_provision else {"rotated": True})

    # ------------------------------------------------------------
    # Portones (GPIO)
    # ------------------------------------------------------------
    @app.route("/gateStatus")
    def gate_status():
        return jsonify(gates.status_all())

    @app.route("/gateStatus/<int:gate_id>")
    def gate_status_id(gate_id):
        try:
            return jsonify({"value": gates.pin_value(gate_id)})
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

    @app.route("/openGate/<int:gate_id>")
    def open_gate(gate_id):
        try:
            gate = gates.open_gate(gate_id)
            return jsonify({"id": gate["id"], "status": gate["status"]})
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

    @app.route("/closeGate/<int:gate_id>")
    def close_gate(gate_id):
        try:
            gate = gates.close_gate(gate_id)
            return jsonify({"id": gate["id"], "status": gate["status"]})
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

    # ------------------------------------------------------------
    # Hikvision (registro dinamico de tarjetas QR)
    # ------------------------------------------------------------
    @app.route("/update-card", methods=["POST"])
    def update_card():
        if hikvision_service is None:
            return jsonify({"error": "Hikvision deshabilitado en este dispositivo"}), 400
        data = request.get_json(silent=True)
        if not data or "cardNo" not in data or "terminals" not in data:
            return jsonify({"error": "cardNo and terminals required"}), 400
        results = hikvision_service.update_card(
            card_no=data["cardNo"],
            employee_no=data.get("employeeNo", "99999"),
            terminals=data["terminals"],
        )
        return jsonify({"cardNo": data["cardNo"], "results": results})

    @app.route("/cleanup-cards", methods=["POST"])
    def cleanup_cards():
        if hikvision_service is None:
            return jsonify({"error": "Hikvision deshabilitado en este dispositivo"}), 400
        return jsonify({"results": hikvision_service.cleanup_all()})

    # ------------------------------------------------------------
    # Estados de pantalla (compatibilidad con el flujo viejo en que el
    # backend llamaba al Pi y este reenviaba a Node 3000)
    # ------------------------------------------------------------
    @app.route("/code-accepted/<string:name>")
    def code_accepted(name):
        screen.accepted(name=name)
        return jsonify({"message": "Access granted"})

    @app.route("/code-denied/<string:name>")
    def code_denied(name):
        screen.denied()
        return jsonify({"message": "Access denied"})

    @app.route("/wait-for-response/<string:name>")
    def wait_for_response(name):
        screen.wait_for_response(name=name)
        return jsonify({"message": "Waiting for response"})

    # ------------------------------------------------------------
    # Mantenimiento remoto
    # ------------------------------------------------------------
    @app.route("/restart", methods=["POST"])
    def restart():
        log.warning("Reinicio del proceso solicitado via /restart")
        threading.Timer(RESTART_RESPONSE_GRACE, restart_process).start()
        return jsonify({"message": "Restarting"})

    return app
