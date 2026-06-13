"""Login de administrador para el editor local (/admin).

Separado del token de dispositivo (auth.py): el token autentica al backend de
Aditum; este login autentica a una PERSONA usando el editor web del Pi, para
que no cualquiera que llegue a http://<pi>/admin pueda ver o cambiar la config.

Las credenciales NUNCA viven en el HTML/JS ni en el codigo: se guardan en
`admin-credentials.json` (gitignored, junto a device-token.txt) como hash
PBKDF2-HMAC-SHA256 + salt. Si el archivo no existe se siembra con un default
(usuario `admin`, password `admin0606`), configurable por env
ADITUM_ADMIN_USER / ADITUM_ADMIN_PASSWORD, y cambiable desde la UI. El default
no esta "quemado": es solo la semilla inicial; al cambiarlo se reescribe el
archivo.

La sesion es una cookie firmada de Flask (HttpOnly); el secreto de firma se
persiste en `admin-session-secret` (gitignored) para sobrevivir reinicios.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time

from flask import session

from .settings import REPO_ROOT

log = logging.getLogger("aditum.admin_auth")

CREDENTIALS_FILE = REPO_ROOT / "admin-credentials.json"
SESSION_SECRET_FILE = REPO_ROOT / "admin-session-secret"

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
SESSION_LIFETIME_SECONDS = 8 * 60 * 60  # 8 horas

PASSWORD_MIN_LEN = 6
PASSWORD_MAX_LEN = 256


def _write_atomic(path, text, mode=0o600):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
        os.chmod(path, mode)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _hash_password(password, salt_hex, iterations=PBKDF2_ITERATIONS):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt_hex), iterations)
    return dk.hex()


def _build_record(username, password):
    salt_hex = secrets.token_hex(SALT_BYTES)
    return {
        "username": username,
        "salt": salt_hex,
        "iterations": PBKDF2_ITERATIONS,
        "hash": _hash_password(password, salt_hex, PBKDF2_ITERATIONS),
    }


def load_credentials():
    """Lee las credenciales; las siembra con el default si no existen."""
    if CREDENTIALS_FILE.exists():
        try:
            return json.loads(CREDENTIALS_FILE.read_text())
        except (OSError, ValueError) as e:
            log.error("admin-credentials.json ilegible (%s): re-sembrando default", e)
    user = os.environ.get("ADITUM_ADMIN_USER", "admin")
    pwd = os.environ.get("ADITUM_ADMIN_PASSWORD", "admin0606")
    rec = _build_record(user, pwd)
    _write_atomic(CREDENTIALS_FILE, json.dumps(rec, indent=2))
    log.warning("Credenciales de admin sembradas (usuario '%s'). Cambialas en /admin.", user)
    return rec


def verify_credentials(username, password):
    rec = load_credentials()
    if not isinstance(username, str) or not isinstance(password, str):
        return False
    user_ok = hmac.compare_digest(username, rec.get("username", ""))
    calc = _hash_password(password, rec["salt"], rec.get("iterations", PBKDF2_ITERATIONS))
    pass_ok = hmac.compare_digest(calc, rec.get("hash", ""))
    return user_ok and pass_ok


def set_credentials(username, password):
    """Reescribe usuario/password (cambio desde la UI)."""
    rec = _build_record(username, password)
    _write_atomic(CREDENTIALS_FILE, json.dumps(rec, indent=2))
    log.warning("Credenciales de admin actualizadas (usuario '%s').", username)


def get_secret_key():
    """Secreto de firma de la cookie de sesion; persistente entre reinicios."""
    if SESSION_SECRET_FILE.exists():
        try:
            val = SESSION_SECRET_FILE.read_text().strip()
            if val:
                return val
        except OSError as e:
            log.error("admin-session-secret ilegible (%s): regenerando", e)
    secret = secrets.token_hex(32)
    _write_atomic(SESSION_SECRET_FILE, secret)
    return secret


def login_session(username):
    session.permanent = True
    session["admin_user"] = username
    session["admin_login_at"] = int(time.time())


def logout_session():
    session.pop("admin_user", None)
    session.pop("admin_login_at", None)


def current_admin():
    """Devuelve el usuario admin logueado y vigente, o None."""
    user = session.get("admin_user")
    if not user:
        return None
    login_at = session.get("admin_login_at", 0)
    if int(time.time()) - int(login_at) > SESSION_LIFETIME_SECONDS:
        return None
    return user


def is_admin_logged_in():
    return current_admin() is not None
