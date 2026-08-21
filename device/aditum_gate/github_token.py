"""Token de GitHub del equipo (para clonar/actualizar con el repo privado).

El token NO vive en el repo ni en la config: vive en /etc/aditum-gate/,
root 600, y lo maneja scripts/github-auth.sh. Este modulo es solo el puente
para que el backend pueda instalarlo por API (PUT /github-token) en vez de
que alguien entre a cada Pi: delega en scripts/set-github-token.sh, que ya
valida contra GitHub antes de guardar y deja la credencial de git aplicada.

Nunca se devuelve ni se loguea el token; solo si esta presente o no.
"""
import logging
import subprocess

from .settings import REPO_ROOT

log = logging.getLogger("aditum.github")

TOKEN_FILE = "/etc/aditum-gate/github-token"
SETTER = REPO_ROOT / "scripts" / "set-github-token.sh"

# El ls-remote contra GitHub es lo lento; con red mala no debe colgar el API
SET_TIMEOUT = 60

TOKEN_MIN_LEN = 20
TOKEN_MAX_LEN = 512


def is_present():
    try:
        with open(TOKEN_FILE) as f:
            return bool(f.read().strip())
    except OSError:
        return False


def validate(token):
    """Devuelve None si el token luce bien, o el motivo del rechazo."""
    if not isinstance(token, str) or not token.strip():
        return "token: requerido"
    if token != token.strip() or any(c.isspace() for c in token):
        return "token: sin espacios"
    if not TOKEN_MIN_LEN <= len(token) <= TOKEN_MAX_LEN:
        return f"token: {TOKEN_MIN_LEN}-{TOKEN_MAX_LEN} caracteres"
    return None


def save(token):
    """Instala el token en el equipo. (ok, detalle) — nunca lanza.

    El token va por stdin, no por argv: la linea de comando la ve cualquiera
    con `ps`. set-github-token.sh lo prueba contra el repo y si no sirve
    devuelve != 0 sin tocar lo que el equipo ya tenia.
    """
    try:
        proc = subprocess.run(
            ["bash", str(SETTER), "-"],
            input=token + "\n",
            capture_output=True,
            text=True,
            timeout=SET_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.error("Instalacion del token de GitHub: timeout de %ss", SET_TIMEOUT)
        return False, "timeout probando el token contra GitHub"
    except OSError as e:
        log.error("Instalacion del token de GitHub fallo: %s", e)
        return False, str(e)

    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = detail[-1] if detail else ""
    if proc.returncode != 0:
        log.error("Token de GitHub RECHAZADO (%s)", detail or proc.returncode)
        return False, detail or f"exit {proc.returncode}"
    log.warning("Token de GitHub instalado por API")
    return True, detail
