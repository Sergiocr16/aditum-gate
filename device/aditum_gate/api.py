"""API Flask del dispositivo (puerto 8080).

Es el "Entry Point" que el backend de Aditum tiene configurado por punto de
acceso. TODOS los endpoints exigen el token del dispositivo (ver auth.py);
solo GET / es publico y minimo. El contrato completo esta en docs/API.md.
"""
import logging
import os
import tempfile
import threading
import time

from datetime import timedelta

import ipaddress

from flask import Flask, jsonify, request, send_from_directory

from . import admin_auth, health
from .anpr import http_status_for
from .anpr_events import parse_event_xml
from .auth import init_auth
from .config_agent import SUPPORTED_SCHEMA_VERSION, apply_config, restart_process
from .settings import DEVICE_ID_FILE, DEVICE_TOKEN_FILE

log = logging.getLogger("aditum.api")

RESTART_RESPONSE_GRACE = 1.0  # segundos para que la respuesta HTTP salga antes del exit

# Atomicidad de PUT /config: el chequeo de deviceId y la reescritura de
# device-id.txt deben ser inseparables del apply (el server es threaded);
# sin esto un push del backend puede evaluar la identidad vieja, esperar
# el lock interno de apply_config y aplicarse DESPUES de que una sesion
# admin re-identifico el equipo, saltandose el 409 anti-intercambio.
_PUT_CONFIG_LOCK = threading.Lock()

TOKEN_MIN_LEN = 16
TOKEN_MAX_LEN = 256


def _write_device_id_file(device_id):
    # Identidad del equipo; escritura atomica como el token (no es secreto)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(DEVICE_ID_FILE.parent), prefix=".device-id.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(device_id + "\n")
        os.replace(tmp_path, DEVICE_ID_FILE)
        os.chmod(DEVICE_ID_FILE, 0o644)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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


def create_app(settings, gates, hikvision_service, screen, leds=None,
               anpr_service=None, anpr_store=None):
    app = Flask(__name__)

    # Sesion del login admin (cookie firmada HttpOnly). El secreto se persiste
    # fuera del repo; sin HTTPS en el Pi, la cookie no es Secure pero si HttpOnly.
    app.secret_key = admin_auth.get_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=admin_auth.SESSION_LIFETIME_SECONDS),
    )
    admin_auth.load_credentials()  # siembra el default si falta

    init_auth(app, settings)

    # ------------------------------------------------------------
    # Publico: health minimo, sin informacion del dispositivo
    # ------------------------------------------------------------
    @app.route("/")
    def root_health():
        # No llamarla "health": sombrearia el modulo health importado arriba
        return jsonify({"status": "ok"})

    @app.route("/admin")
    def admin_page():
        # Editor local de configuracion (http://localhost:8080/admin).
        # El HTML es publico pero su contenido se tapa con el login; los datos
        # (GET/PUT /config, etc.) exigen sesion admin o token de dispositivo.
        # no-store: el editor se actualiza via self-update y un HTML cacheado
        # deja al usuario viendo un formulario viejo (p.ej. sin validaciones).
        resp = send_from_directory(
            os.path.join(os.path.dirname(__file__), "static"), "admin.html"
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------
    # Login de administrador del editor local (sesion por cookie)
    # ------------------------------------------------------------
    @app.route("/admin/session")
    def admin_session():
        user = admin_auth.current_admin()
        return jsonify({"authenticated": bool(user), "username": user})

    @app.route("/admin/login", methods=["POST"])
    def admin_login():
        data = request.get_json(silent=True) or {}
        username = data.get("username", "")
        password = data.get("password", "")
        time.sleep(0.3)  # freno suave anti fuerza bruta
        if not admin_auth.verify_credentials(username, password):
            log.warning("Login admin fallido para usuario '%s'", username)
            return jsonify({"error": "credenciales invalidas"}), 401
        admin_auth.login_session(username)
        log.info("Login admin OK: %s", username)
        return jsonify({"ok": True, "username": username})

    @app.route("/admin/logout", methods=["POST"])
    def admin_logout():
        admin_auth.logout_session()
        return jsonify({"ok": True})

    @app.route("/admin/password", methods=["POST"])
    def admin_password():
        # Cambiar la contraseña (exige sesion admin vigente; lo garantiza el
        # before_request al no estar /admin/password en PUBLIC_PATHS).
        if not admin_auth.is_admin_logged_in():
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or admin_auth.current_admin() or "").strip()
        current = data.get("currentPassword", "")
        new = data.get("newPassword", "")
        if not admin_auth.verify_credentials(admin_auth.current_admin(), current):
            return jsonify({"error": "contraseña actual incorrecta"}), 403
        if (not isinstance(new, str)
                or not admin_auth.PASSWORD_MIN_LEN <= len(new) <= admin_auth.PASSWORD_MAX_LEN):
            return jsonify({
                "error": "contraseña invalida",
                "details": [f"{admin_auth.PASSWORD_MIN_LEN}-{admin_auth.PASSWORD_MAX_LEN} caracteres"],
            }), 400
        if not username:
            return jsonify({"error": "usuario requerido"}), 400
        admin_auth.set_credentials(username, new)
        admin_auth.login_session(username)  # refresca la sesion con el usuario nuevo
        return jsonify({"ok": True, "username": username})

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

    @app.route("/health")
    def health_report():
        # Salud del dispositivo: USB conectados, lectores, servicios, sistema.
        # Protegido por el before_request global (sesion admin o token).
        return jsonify(health.report(settings))

    @app.route("/config")
    def get_config():
        return jsonify({
            "config": settings.raw,
            "source": settings.source.name if settings.source else None,
        })

    @app.route("/backup")
    def get_backup():
        # Devuelve el documento de configuracion EXACTAMENTE en el formato de
        # exportacion del editor (Respaldo -> Exportar), con la identidad
        # efectiva (device-id.txt manda sobre una config pushada generica).
        # Aditum lo guarda tal cual en su BD; restaurar = PUT /config con el
        # mismo JSON, o importarlo en /admin (aplica y reinicia solo).
        doc = dict(settings.raw)
        if settings.device_id:
            doc["deviceId"] = settings.device_id
        return jsonify(doc)

    @app.route("/config", methods=["PUT"])
    def put_config():
        new_config = request.get_json(silent=True)
        if not isinstance(new_config, dict):
            return jsonify({"error": "body must be a JSON object"}), 400

        # Defensa contra entry points intercambiados: la config de otra Pi
        # no se aplica. deviceId vacio/ausente = config generica, se acepta.
        # Excepcion: la sesion admin del editor local si puede re-identificar
        # el equipo (reescribe device-id.txt); un push con token no.
        with _PUT_CONFIG_LOCK:
            body_device_id = new_config.get("deviceId") or ""
            wants_new_id = (isinstance(body_device_id, str)
                            and body_device_id != settings.device_id)
            rewrite_id = False
            if wants_new_id and body_device_id:
                if admin_auth.is_admin_logged_in():
                    if len(body_device_id) > 128 or any(c.isspace() for c in body_device_id):
                        return jsonify({
                            "error": "invalid config",
                            "details": ["deviceId: maximo 128 caracteres, sin espacios"],
                        }), 400
                    rewrite_id = True
                elif settings.device_id:
                    return jsonify({
                        "error": "deviceId mismatch",
                        "expected": settings.device_id,
                    }), 409

            result = apply_config(new_config, settings)
            if result.get("error"):
                http_status = 400 if result["error"] == "invalid config" else 409
                return jsonify(result), http_status

            if rewrite_id:
                _write_device_id_file(body_device_id)
                log.warning("deviceId re-identificado via sesion admin: %r -> %r",
                            settings.device_id, body_device_id)
                settings.device_id = body_device_id

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

    @app.route("/token", methods=["DELETE"])
    def delete_token():
        # Desprovisiona el equipo: borra device-token.txt y vuelve al estado
        # TOFU (PUT /token abierto de nuevo; el resto exige sesion admin).
        # Accion autenticada (token vigente o sesion admin) e idempotente;
        # el backend la usa al desvincular o re-provisionar un equipo.
        try:
            os.unlink(DEVICE_TOKEN_FILE)
        except FileNotFoundError:
            pass
        settings.device_token = ""  # efecto inmediato (auth lo lee por request)
        log.warning("Token de dispositivo ELIMINADO: equipo sin provisionar (TOFU abierto)")
        return jsonify({"deprovisioned": True})

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
    # ANPR local-first (TAR-1034/TAR-1035): la lista de placas vive en la
    # camara; Aditum la mantiene via /update-plate y /sync-plates, y las
    # lecturas llegan por /anpr-event y se reenvian con cola offline.
    # ------------------------------------------------------------

    def _camera_source_ip():
        # Detras del nginx local la IP real de la camara viene en
        # X-Forwarded-For; directo contra :8080 es remote_addr.
        if request.remote_addr in ("127.0.0.1", "::1"):
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.remote_addr or ""

    def _camera_ip_allowed(ip):
        # Best-effort (TAR-1035): si hay camaras declaradas en la config,
        # solo esas; si no hay ninguna, cualquier IP privada de la LAN del
        # condominio (equipo recien instalado, todavia sin mapear).
        declared = settings.anpr_camera_allowed(ip)
        if declared is not None:
            return declared
        try:
            return ipaddress.ip_address(ip).is_private
        except ValueError:
            return False

    def _extract_event_xml(req):
        # La camara postea multipart/form-data con el XML en una parte
        # (tipicamente anpr.xml) + jpgs; tambien se acepta XML crudo.
        if req.files:
            for part in req.files.values():
                content_type = part.content_type or ""
                filename = part.filename or ""
                if "xml" in content_type or filename.endswith(".xml"):
                    return part.read()
            first = next(iter(req.files.values()), None)
            if first is not None:
                return first.read()
        return req.get_data() or None

    @app.route("/update-plate", methods=["POST"])
    def update_plate():
        # Contrato: CONTRACT.md de TAR-1033 (aditum-jh). Solo el codigo HTTP
        # decide exito/fallo para el backend; el body es informativo.
        if anpr_service is None:
            return jsonify({"error": "ANPR deshabilitado en este dispositivo"}), 400
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        plate_normalized = data.get("plateNormalized")
        cameras = data.get("cameras")
        if action not in ("ADD", "DELETE") or not plate_normalized or not cameras:
            return jsonify({"error": "action, plateNormalized and cameras required"}), 400
        # Nunca loguear el request completo (trae credenciales de camara)
        log.info("update-plate: requestId=%s action=%s cameraId=%s plate=%s",
                 data.get("requestId"), action, data.get("cameraId"),
                 plate_normalized)
        results, error_code = anpr_service.update_plate(
            action, plate_normalized, cameras)
        applied = sum(1 for r in results if r["ok"])
        body = {
            "ok": error_code is None and applied > 0,
            "action": action,
            "plateNormalized": plate_normalized,
            "applied": applied,
            "failed": len(results) - applied,
            "results": results,
        }
        if not results:
            return jsonify({"error": "cameras sin ip valida"}), 400
        if error_code:
            body["error"] = error_code
            return jsonify(body), http_status_for(error_code)
        return jsonify(body)

    @app.route("/sync-plates", methods=["POST"])
    def sync_plates():
        # Full sync (TAR-1037 -> TAR-1034): deja la camara EXACTAMENTE con
        # las placas del payload (reemplazo completo, idempotente).
        if anpr_service is None:
            return jsonify({"error": "ANPR deshabilitado en este dispositivo"}), 400
        data = request.get_json(silent=True) or {}
        plates = data.get("plates")
        cameras = data.get("cameras")
        if not isinstance(plates, list) or not cameras:
            return jsonify({"error": "plates and cameras required"}), 400
        normalized = [p.get("plateNormalized") for p in plates
                      if isinstance(p, dict) and p.get("plateNormalized")]
        log.info("sync-plates: requestId=%s cameraId=%s placas=%s",
                 data.get("requestId"), data.get("cameraId"), len(normalized))
        results, error_code = anpr_service.sync_plates(normalized, cameras)
        applied = sum(1 for r in results if r["ok"])
        body = {
            "ok": error_code is None and applied > 0,
            "plates": len(normalized),
            "applied": applied,
            "failed": len(results) - applied,
            "results": results,
        }
        if not results:
            return jsonify({"error": "cameras sin ip valida"}), 400
        if error_code:
            body["error"] = error_code
            return jsonify(body), http_status_for(error_code)
        return jsonify(body)

    @app.route("/anpr-event", methods=["POST"])
    def anpr_event():
        # PUBLICO (la camara no sabe mandar bearer; ver PUBLIC_PATHS en
        # auth.py): filtra por IP de origen y SOLO encola — nunca abre
        # portones ni toca configuracion. Responde 200 tambien a heartbeats
        # para que la camara no reintente basura.
        if anpr_store is None:
            return jsonify({"error": "ANPR deshabilitado en este dispositivo"}), 404
        source_ip = _camera_source_ip()
        if not _camera_ip_allowed(source_ip):
            log.warning("Evento ANPR rechazado desde IP no permitida: %s",
                        source_ip)
            return jsonify({"error": "forbidden"}), 403
        xml_bytes = _extract_event_xml(request)
        if not xml_bytes:
            return jsonify({"error": "empty body"}), 400
        event = parse_event_xml(xml_bytes)
        if event is None:
            return jsonify({"ignored": True})
        gate_id = settings.anpr_gate_id_for(source_ip)
        queued = anpr_store.enqueue(event, source_ip=source_ip, gate_id=gate_id)
        log.info("Evento ANPR %s: placa=%s capturado=%s porton=%s (%s)",
                 event["eventUid"], event["licensePlate"], event["capturedAt"],
                 gate_id if gate_id is not None else "sin mapear",
                 "encolado" if queued else "duplicado ignorado")
        return jsonify({"queued": queued, "eventUid": event["eventUid"]})

    @app.route("/anpr-status")
    def anpr_status():
        # Protegido por el before_request global; para soporte y el piloto.
        if anpr_store is None:
            return jsonify({"error": "ANPR deshabilitado en este dispositivo"}), 404
        return jsonify(anpr_store.stats())

    # ------------------------------------------------------------
    # Estados de pantalla y LED (compatibilidad con el flujo viejo en que
    # el backend llamaba al Pi y este reenviaba a Node 3000; la tira
    # NeoPixel acompaña cada estado como en los pedestales originales)
    # ------------------------------------------------------------
    @app.route("/code-accepted/<string:name>")
    def code_accepted(name):
        screen.accepted(name=name)
        if leds:
            leds.flash_green(seconds=4)
        return jsonify({"message": "Access granted"})

    @app.route("/code-denied/<string:name>")
    def code_denied(name):
        screen.denied()
        if leds:
            leds.flash_red(seconds=4)
        return jsonify({"message": "Access denied"})

    @app.route("/wait-for-response/<string:name>")
    def wait_for_response(name):
        screen.wait_for_response(name=name)
        if leds:
            leds.start_blinking()
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
