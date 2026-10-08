"""API Flask del dispositivo (puerto 8080).

Es el "Entry Point" que el backend de Aditum tiene configurado por punto de
acceso. TODOS los endpoints exigen el token del dispositivo (ver auth.py);
solo GET / es publico y minimo. El contrato completo esta en docs/API.md.
"""
import email.parser
import logging
import os
import re
import socket
import socketserver
import tempfile
import threading
import time

from datetime import timedelta

import ipaddress

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.serving import ThreadedWSGIServer

from . import admin_auth, github_token, health, maintenance
from .anpr import AnprCameraError, http_status_for, normalize_plate
from .anpr_captures import REASON_NOT_AUTHORIZED, REASON_UNREADABLE
from .anpr_events import parse_event_xml, is_authorized
from .auth import init_auth
from .config_agent import SUPPORTED_SCHEMA_VERSION, apply_config, restart_process
from .hikvision import normalize_card_nos
from .settings import DEVICE_ID_FILE, DEVICE_TOKEN_FILE

log = logging.getLogger("aditum.api")

RESTART_RESPONSE_GRACE = 1.0  # segundos para que la respuesta HTTP salga antes del exit

# Si el API no escucha en este plazo desde el arranque, el proceso sale para
# que PM2 lo relance: PM2 solo ve "online", no si el puerto atiende.
API_LISTEN_TIMEOUT = 60


class ApiServer(ThreadedWSGIServer):
    """Servidor del API sin la consulta DNS inversa del bind.

    http.server.HTTPServer.server_bind hace socket.getfqdn(host) ENTRE el
    bind y el listen. Con el DNS del equipo caido o lento, esa consulta
    bloquea al hilo principal con el puerto reservado pero sin escuchar:
    nginx recibe "connection refused" y responde 503, los demas threads
    (poller, lectores) siguen logueando normal, PM2 ve el proceso online y
    nadie lo relanza. Visto en campo: pedestal pegado en "Reiniciando el
    servicio" hasta un reboot. El nombre del server no se usa para nada:
    se fija al host sin resolver.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def _listen_watchdog(port):
    """Sale del proceso si nadie atiende el puerto pasado API_LISTEN_TIMEOUT."""
    time.sleep(API_LISTEN_TIMEOUT)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return
    except OSError as e:
        log.error("El API no escucha en :%s tras %ss (%s): saliendo para que "
                  "PM2 relance el proceso", port, API_LISTEN_TIMEOUT, e)
        logging.shutdown()
        os._exit(1)


def serve(app, host, port):
    """Atiende el API hasta SIGINT (PM2) — reemplaza a app.run().

    Threaded: el API DEPENDE de atender requests concurrentes (un pulso de
    porton de 1 s o un ISAPI a Hikvision no pueden bloquear el health check).
    """
    threading.Thread(target=_listen_watchdog, args=(port,),
                     name="api-listen-watchdog", daemon=True).start()
    server = ApiServer(host, port, app)
    log.info("API escuchando en %s:%s", host, port)
    server.serve_forever()

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
               anpr_service=None, anpr_store=None, anpr_captures=None):
    app = Flask(__name__)

    # Sesion del login admin (cookie firmada HttpOnly). El secreto se persiste
    # fuera del repo; sin HTTPS en el Pi, la cookie no es Secure pero si HttpOnly.
    app.secret_key = admin_auth.get_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=admin_auth.SESSION_LIFETIME_SECONDS),
        # Cuerpos grandes: el evento ANPR trae dos JPG (hasta ~1 MB cada
        # uno) y /sync-plates la lista entera. nginx ya corta en 10 MB; este
        # es el tope hablando directo al :8080. MAX_FORM_MEMORY_SIZE sube del
        # default de 500 KB porque, si la camara manda las fotos sin
        # `filename=`, Werkzeug las trata como campos de formulario en
        # memoria y con el default responderia 413 y se perderia el evento
        # entero (ver _raw_multipart_parts).
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=16 * 1024 * 1024,
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
    def device_status():
        """Identidad y flags del equipo.

        Sale por dos vias y por eso vive en un solo lugar: GET /status (lo
        que Aditum consulta de la flota) y el bloque "device" de GET /health
        (lo mismo, pero al lado de servicios/lectores/sistema para el tecnico
        que mira el editor local). Agregar un campo aca lo publica en ambas.
        """
        return {
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
            "anprEnabled": settings.anpr_enabled,
            # Para armar la URL que se le configura a la camara ANPR
            "lanIp": health.lan_ipv4(),
            # Solo presencia: si es false y el repo es privado, este equipo
            # ya no se actualiza (ver PUT /github-token)
            "githubToken": github_token.is_present(),
        }

    @app.route("/status")
    def status():
        # Version del codigo y atraso contra el ultimo fetch (sin red). En
        # /health el mismo bloque va en la raiz, no dentro de "device".
        return jsonify(dict(device_status(), code=health.code_status()))

    @app.route("/health")
    def health_report():
        # Salud del dispositivo: USB conectados, lectores, servicios, sistema
        # y el mismo "device" que devuelve /status (identidad, flags, version).
        # Protegido por el before_request global (sesion admin o token).
        report = health.report(settings)
        report["device"] = device_status()
        return jsonify(report)

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

    @app.route("/github-token", methods=["PUT"])
    def put_github_token():
        # Instala el token con el que ESTE equipo lee el repo (self-update).
        # Lo manda el backend, que lo tiene como variable de entorno, para no
        # tener que entrar equipo por equipo. Protegido por el before_request
        # global (token del dispositivo o sesion admin) y de una sola via: no
        # existe GET, el token nunca se devuelve ni se loguea.
        data = request.get_json(silent=True) or {}
        token = data.get("token", "")
        problem = github_token.validate(token)
        if problem:
            return jsonify({"error": "invalid token", "details": [problem]}), 400

        replaced = github_token.is_present()
        ok, detail = github_token.save(token)
        if not ok:
            # No se toco lo que el equipo tenia: el setter valida antes
            return jsonify({"error": "token rejected", "details": [detail]}), 400
        return jsonify({"saved": True, "replaced": replaced, "repoReadable": True})

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
        # Dos payloads: legacy {cardNo} = reemplazo total; nuevo {cardNo,
        # cardNos: [vigente, +1, +2]} = el conjunto completo de tarjetas que
        # deben quedar vivas (cardNo sigue viajando por compatibilidad y es
        # cardNos[0]). Ver hikvision.py y docs/API.md.
        if hikvision_service is None:
            return jsonify({"error": "Hikvision deshabilitado en este dispositivo"}), 400
        data = request.get_json(silent=True)
        if not data or "terminals" not in data:
            return jsonify({"error": "cardNo and terminals required"}), 400
        card_nos = None
        if "cardNos" in data:
            raw = data["cardNos"]
            if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
                return jsonify({"error": "cardNos must be a list of strings"}), 400
            card_nos = normalize_card_nos(raw)
            if not card_nos:
                return jsonify({"error": "cardNos must not be empty"}), 400
        card_no = data.get("cardNo") or (card_nos[0] if card_nos else None)
        if not card_no:
            return jsonify({"error": "cardNo and terminals required"}), 400
        started = time.perf_counter()
        results = hikvision_service.update_card(
            card_no=card_no,
            employee_no=data.get("employeeNo", "99999"),
            terminals=data["terminals"],
            card_nos=card_nos,
        )
        # Tiempo total del Pi (terminales en paralelo); cada result trae el suyo
        body = {"cardNo": card_no, "results": results,
                "elapsedMs": int((time.perf_counter() - started) * 1000)}
        if card_nos is not None:
            body["cardNos"] = card_nos
        return jsonify(body)

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
        # Best-effort (TAR-1035): la LAN del condominio. NO se declaran las
        # camaras aca a proposito: su direccion es de Aditum (anpr_camera,
        # TAR-1030) y duplicarla en cada Pi seria una segunda fuente de
        # verdad que se desincroniza al cambiar una IP.
        try:
            return ipaddress.ip_address(ip).is_private
        except ValueError:
            return False

    def _raw_multipart_parts(req):
        # Respaldo tolerante al formato de la camara: Werkzeug solo trata una
        # parte como archivo si trae `filename=` en Content-Disposition; hay
        # firmwares Hikvision que mandan `name="detectionPicture.jpg"` a
        # secas, y asi el JPG caeria en request.form decodificado como texto
        # (irrecuperable). El parser de email lee el cuerpo crudo por
        # boundary sin esa exigencia y devuelve los bytes intactos. El cuerpo
        # esta cacheado porque el handler llama get_data() antes de tocar
        # request.files. Devuelve [(nombre, content_type, bytes)].
        content_type = req.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/"):
            return []
        head = ("Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n" % content_type).encode("latin-1", "replace")
        try:
            msg = email.parser.BytesParser().parsebytes(head + req.get_data())
        except Exception as e:
            log.warning("No se pudo parsear el multipart crudo del evento ANPR: %s", e)
            return []
        parts = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            name = (part.get_param("filename", header="content-disposition")
                    or part.get_param("name", header="content-disposition") or "")
            payload = part.get_payload(decode=True) or b""
            parts.append((str(name), part.get_content_type().lower(), payload))
        return parts

    def _extract_event_xml(req):
        # La camara postea multipart/form-data con el XML en una parte
        # (tipicamente anpr.xml) + jpgs; tambien se acepta XML crudo.
        if req.files:
            for part in req.files.values():
                content_type = part.content_type or ""
                filename = part.filename or ""
                if "xml" in content_type or filename.endswith(".xml"):
                    return part.read()
        for name, content_type, data in _raw_multipart_parts(req):
            if "xml" in content_type or name.lower().endswith(".xml"):
                return data
        if req.files:
            first = next(iter(req.files.values()), None)
            if first is not None:
                return first.read()
        # Sin parte XML reconocible: el cuerpo tal cual (XML crudo, o un
        # multipart raro que no parsea y termina en {"ignored": true}, como
        # siempre: a la camara nunca se le responde 400 por basura).
        return req.get_data() or None

    def _is_image_part(name, content_type):
        return "image" in content_type or name.lower().endswith((".jpg", ".jpeg"))

    def _extract_event_images(req):
        # Las fotos del evento (licensePlatePicture.jpg = recorte de la
        # placa, detectionPicture.jpg = escena) viajan como partes del mismo
        # multipart. items(multi=True): la camara puede repetir el nombre.
        images = []
        for key, part in req.files.items(multi=True):
            if not _is_image_part(part.filename or key or "", (part.content_type or "").lower()):
                continue
            try:
                part.stream.seek(0)
            except (AttributeError, OSError, ValueError):
                pass
            images.append((part.filename or key, part.read()))
        if not images:
            # Partes sin filename= (ver _raw_multipart_parts)
            images = [(name, data) for name, content_type, data in _raw_multipart_parts(req)
                      if _is_image_part(name, content_type) and data]
        return images

    def _capture_unrecognized(event, source_ip, reason):
        # Bitacora local con foto (anpr_captures). Nunca puede tumbar el
        # handler: la cola de eventos sigue aunque el disco falle.
        if anpr_captures is None:
            return
        try:
            images = _extract_event_images(request)
            declared = event.get("pictureCount")
            if declared and not images:
                # La camara dice en el XML (<picNum>) que adjunto fotos y no
                # llegaron: casi siempre es la camara posteando sin "picture"
                # o un proxy que recorto el cuerpo. Es lo que hay que mirar
                # en sitio cuando la bitacora sale sin fotos.
                log.warning("Evento ANPR %s declara %s foto(s) en el XML pero el "
                            "multipart no trajo ninguna (Content-Type=%s, %s bytes)",
                            event.get("eventUid"), declared, request.mimetype,
                            request.content_length)
            anpr_captures.save(event, images, source_ip=source_ip, reason=reason)
        except Exception:
            log.exception("No se pudo guardar la placa no reconocida")

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
        # Cachear el cuerpo crudo ANTES de que Werkzeug parsee el form: asi
        # _raw_multipart_parts puede releerlo si las partes vienen sin filename.
        request.get_data()
        xml_bytes = _extract_event_xml(request)
        if not xml_bytes:
            return jsonify({"error": "empty body"}), 400
        event = parse_event_xml(xml_bytes)
        if event is None:
            return jsonify({"ignored": True})
        if event["licensePlate"] is None:
            # Placa ilegible ("unknown"): no hay nada que encolar, pero la
            # foto es justo lo que hace falta para saber que vio la camara.
            _capture_unrecognized(event, source_ip, REASON_UNREADABLE)
            log.info("Evento ANPR sin placa legible desde=%s", source_ip)
            return jsonify({"ignored": "placa ilegible"})
        if not is_authorized(event):
            # Fuera del allow list: se guarda con foto haya o no filtro, para
            # poder revisar en sitio si fue una lectura mala de una placa
            # que si esta autorizada.
            _capture_unrecognized(event, source_ip, REASON_NOT_AUTHORIZED)
        # Switch anpr.onlyAuthorized (default true): solo se encolan lecturas
        # del allow list (whiteList). En false se encolan todas para revisar.
        if settings.anpr_only_authorized and not is_authorized(event):
            # Se cuenta y se loguea: una lectura descartada no deja rastro en
            # la cola (la bitacora es solo de autorizadas), asi que sin esto
            # el filtro seria imposible de verificar desde el equipo.
            anpr_store.record_list_outcome(event["vehicleList"], discarded=True)
            # Se guarda ademas como fila 'discarded' (nunca se reenvia, la
            # purga se la lleva igual que a las confirmadas) para poder
            # responder desde /admin por que una placa no esta en la bitacora.
            anpr_store.record_discarded(event, source_ip=source_ip)
            log.info("Evento ANPR descartado por allowlist: placa=%s lista=%r "
                     "desde=%s", event["licensePlate"], event["vehicleList"],
                     source_ip)
            return jsonify({"ignored": "no autorizada",
                            "vehicleList": event["vehicleList"]})
        anpr_store.record_list_outcome(event["vehicleList"], discarded=False)
        queued = anpr_store.enqueue(event, source_ip=source_ip)
        log.info("Evento ANPR %s: placa=%s capturado=%s desde=%s (%s)",
                 event["eventUid"], event["licensePlate"], event["capturedAt"],
                 source_ip, "encolado" if queued else "duplicado ignorado")
        return jsonify({"queued": queued, "eventUid": event["eventUid"]})

    @app.route("/anpr-test", methods=["POST"])
    def anpr_test():
        # Diagnostico de instalacion (protegido por el before_request global):
        # prueba la conexion Pi -> camara sin pasar por Aditum. Las credenciales
        # se reciben aca SOLO para la prueba: no se guardan ni se loguean, igual
        # que las que manda el backend en cada despacho.
        if anpr_service is None:
            return jsonify({"error": "ANPR deshabilitado en este dispositivo"}), 400
        data = request.get_json(silent=True) or {}
        ip = (data.get("ip") or "").strip()
        action = (data.get("action") or "CHECK").upper()
        if not ip:
            return jsonify({"error": "ip requerida"}), 400
        # La camara vive en la LAN del condominio. Sin este guard el endpoint
        # convierte al Pi en un proxy para golpear cualquier host de internet
        # con las credenciales que le pasen (SSRF). Se valida solo el host:
        # la IP puede venir con puerto (192.168.1.64:8000), que es como se
        # configura una camara detras de un NAT o en un puerto no estandar.
        host = ip.rsplit(":", 1)[0] if ip.count(":") == 1 else ip
        try:
            if not ipaddress.ip_address(host).is_private:
                return jsonify({"error": "la IP debe ser de la red local"}), 400
        except ValueError:
            return jsonify({"error": "IP invalida"}), 400
        if action not in ("CHECK", "LIST", "FIND", "ADD", "DELETE"):
            return jsonify(
                {"error": "action debe ser CHECK, LIST, FIND, ADD o DELETE"}), 400

        user = data.get("user") or "admin"
        password = data.get("password") or ""
        client = anpr_service.client
        try:
            if action == "CHECK":
                result = client.probe(ip, user, password)
                log.info("anpr-test CHECK %s -> %s placas", ip, result["plates"])
                return jsonify(dict(result, ok=True, action=action))
            if action == "LIST":
                # Solo lectura: exporta la lista de la camara para consultarla.
                result = client.list_plates(ip, user, password)
                log.info("anpr-test LIST %s -> %s placas%s", ip, result["total"],
                         " (truncada)" if result["truncated"] else "")
                return jsonify(dict(result, ok=True, action=action))
            plate = normalize_plate(data.get("plate"))
            if not plate:
                return jsonify(
                    {"error": "placa requerida para FIND, ADD o DELETE"}), 400
            if action == "FIND":
                # Solo lectura: responde si la placa esta en la camara y en que
                # lista, sin tocar nada.
                result = client.find_plate(ip, user, password, plate)
                log.info("anpr-test FIND %s en %s -> %s", plate, ip,
                         "encontrada" if result["found"] else "no esta")
                return jsonify(dict(result, ok=True, action=action,
                                    plateNormalized=plate))
            detail = client.apply_plate(ip, user, password, action, plate)
            log.info("anpr-test %s %s en %s -> %s", action, plate, ip, detail)
            return jsonify({"ok": True, "action": action, "plateNormalized": plate,
                            "detail": detail})
        except AnprCameraError as e:
            log.warning("anpr-test %s en %s fallo: %s", action, ip, e.code)
            # message puede nombrar la IP, nunca usuario ni contrasena
            return jsonify({"ok": False, "action": action, "error": e.code,
                            "message": e.message}), http_status_for(e.code)

    @app.route("/anpr-status")
    def anpr_status():
        # Protegido por el before_request global; para soporte y el piloto.
        if anpr_store is None:
            return jsonify({"error": "ANPR deshabilitado en este dispositivo"}), 404
        # onlyAuthorized viaja con las estadisticas: sin saber si el filtro
        # esta encendido, los contadores por lista no se pueden interpretar.
        return jsonify(dict(anpr_store.stats(),
                            onlyAuthorized=settings.anpr_only_authorized))

    @app.route("/anpr-status/pending", methods=["DELETE"])
    def anpr_clear_pending():
        # Protegido por el before_request global. Borra las lecturas PENDIENTES
        # de enviar (no toca las confirmadas ni las descartadas). Para limpiar la
        # cola tras pruebas o un cutover, desde el editor. Matchea el regex nginx
        # de /anpr-status, no necesita ruta nueva.
        if anpr_store is None:
            return jsonify({"error": "ANPR deshabilitado en este dispositivo"}), 404
        deleted = anpr_store.delete_pending()
        log.warning("Cola ANPR: %s pendientes borradas via /anpr-status/pending", deleted)
        return jsonify({"deleted": deleted})

    # ------------------------------------------------------------
    # Bitacora local de placas NO reconocidas (anpr_captures): JSON + fotos
    # de la camara, en disco. Diagnostico: nada de esto sale hacia Aditum.
    # ------------------------------------------------------------

    def _capture_filters(args):
        """Query string del visor -> filtros del store. Lo invalido se ignora
        (no se filtra por ese campo) en vez de responder 400: es una pantalla
        de diagnostico, no un contrato con el backend."""
        filters = {}
        date = (args.get("date") or "").replace("-", "")
        if re.fullmatch(r"[0-9]{8}", date):
            filters["date"] = date
        for key, name, fill in (("from", "time_from", "00"), ("to", "time_to", "59")):
            raw = args.get(key) or ""
            m = re.fullmatch(r"([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?", raw)
            if m:
                filters[name] = m.group(1) + m.group(2) + (m.group(3) or fill)
        plate = re.sub(r"[^A-Z0-9-]", "", (args.get("plate") or "").upper())[:16]
        if plate:
            filters["plate"] = plate
        if args.get("reason") in (REASON_UNREADABLE, REASON_NOT_AUTHORIZED):
            filters["reason"] = args.get("reason")
        return filters

    @app.route("/anpr-captures")
    def anpr_captures_list():
        # Lista paginada y filtrable (fecha, rango de horas, placa, motivo)
        # para el visor del editor. Los filtros van sobre la hora en que la
        # Pi RECIBIO el evento (la del nombre de archivo), que es la misma
        # de la camara salvo reloj desfasado; capturedAt viaja igual.
        if anpr_captures is None:
            return jsonify({"error": "Bitacora de placas no reconocidas deshabilitada"}), 404
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 500))
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            limit, offset = 50, 0
        filters = _capture_filters(request.args)
        captures, total = anpr_captures.list(limit, offset, **filters)
        return jsonify(dict(anpr_captures.stats(), captures=captures, total=total,
                            limit=limit, offset=offset, filters=filters,
                            days=anpr_captures.days()))

    @app.route("/anpr-captures/<name>")
    def anpr_capture_file(name):
        # Sirve una foto (o el JSON) por nombre. El nombre se valida contra
        # el patron que genera el propio modulo: no hay forma de salir del
        # directorio ni de leer otro archivo.
        if anpr_captures is None:
            return jsonify({"error": "Bitacora de placas no reconocidas deshabilitada"}), 404
        path = anpr_captures.file_path(name)
        if path is None:
            return jsonify({"error": "not found"}), 404
        resp = send_from_directory(str(anpr_captures.dir), path.name)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    @app.route("/anpr-captures", methods=["DELETE"])
    def anpr_captures_clear():
        if anpr_captures is None:
            return jsonify({"error": "Bitacora de placas no reconocidas deshabilitada"}), 404
        deleted = anpr_captures.clear()
        log.warning("Bitacora de placas no reconocidas: %s registro(s) borrados "
                    "via DELETE /anpr-captures", deleted)
        return jsonify({"deleted": deleted})

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

    @app.route("/restart-server", methods=["POST"])
    def restart_server():
        # Reinicia los DOS procesos PM2. /restart solo se reinicia a si mismo
        # (os._exit): esto tambien levanta aditum-web, o sea la pantalla
        # colgada, que es el caso que /restart no arregla. El SO no se toca.
        # Se verifica pm2 ANTES de responder: contestar "Restarting" y que no
        # pase nada es peor que un error, es lo ultimo que se intenta antes
        # de mandar a alguien al sitio.
        if not maintenance.locate_pm2():
            return jsonify({"error": "pm2 no disponible en este equipo"}), 503
        log.warning("Reinicio de los servicios solicitado via /restart-server")
        threading.Timer(RESTART_RESPONSE_GRACE, maintenance.restart_services).start()
        return jsonify({
            "message": "Restarting services",
            "processes": list(maintenance.PM2_PROCESS_NAMES),
        })

    @app.route("/reboot", methods=["POST"])
    def reboot():
        # Reinicia el EQUIPO entero: el acceso queda caido hasta que bootee.
        # No confundir con el watchdog de red, que hace lo mismo por su
        # cuenta cuando pierde conectividad (ahi no hay quien llame a esto).
        if not maintenance.locate_reboot():
            return jsonify({"error": "reboot no disponible en este equipo"}), 503
        log.warning("Reinicio del EQUIPO solicitado via /reboot")
        threading.Timer(RESTART_RESPONSE_GRACE, maintenance.reboot_system).start()
        return jsonify({"message": "Rebooting"})

    return app
