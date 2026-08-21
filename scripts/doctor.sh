#!/bin/bash
# Diagnostico integral del equipo aditum-gate: verifica que todo lo que la
# variante configurada necesita esta instalado, en la version pinneada y
# funcionando. No cambia nada (solo lee). Exit 0 = sano, 1 = hay FAILs.
#
# Uso:  bash scripts/doctor.sh          (con sudo ve tambien PM2 root)
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

PASS=0; FAIL=0; WARN=0
ok()   { echo "  OK    $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
warn() { echo "  WARN  $1"; WARN=$((WARN+1)); }
section() { echo; echo "== $1"; }

PY=.venv/bin/python3

# ------------------------------------------------------------------
section "Sistema"
# ------------------------------------------------------------------
model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo desconocido)"
os="$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo desconocido)"
echo "  info  $model | $os | $(uname -m)"
pyver="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo 0)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \
  && ok "python3 $pyver (>= 3.9)" || bad "python3 $pyver (< 3.9: OS demasiado viejo)"

# ------------------------------------------------------------------
section "Repo y auto-update"
# ------------------------------------------------------------------
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
commit="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
expected_branch="${ADITUM_BRANCH:-production}"
[ -f /etc/default/aditum-gate ] && . /etc/default/aditum-gate 2>/dev/null || true
if [ "$branch" = "${ADITUM_BRANCH:-$expected_branch}" ]; then
  ok "branch $branch @ $commit"
else
  warn "branch $branch @ $commit (la flota trackea ${ADITUM_BRANCH:-$expected_branch})"
fi
if systemctl is-active --quiet aditum-update.timer 2>/dev/null; then
  ok "aditum-update.timer activo"
else
  bad "aditum-update.timer inactivo (el equipo no se actualiza solo)"
fi
# Acceso de lectura al repo remoto: si falla, el equipo se congela en la
# version que tenga (el timer corre pero no trae nada).
# shellcheck source=scripts/github-auth.sh
. "$REPO_DIR/scripts/github-auth.sh" 2>/dev/null || true
if github_token_present 2>/dev/null; then
  perms="$(stat -c '%a %U' "$GH_TOKEN_FILE" 2>/dev/null)"
  if [ "$perms" = "600 root" ]; then
    ok "token de GitHub presente ($GH_TOKEN_FILE, root 600)"
  else
    warn "token de GitHub con permisos $perms (deberia ser 600 root)"
  fi
else
  warn "sin token de GitHub: si el repo pasa a privado este equipo deja de actualizarse"
fi
remote_url="$(git remote get-url origin 2>/dev/null || echo '')"
if [ -n "$remote_url" ] && GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true \
     git ls-remote --exit-code origin HEAD >/dev/null 2>&1; then
  ok "el repo remoto se puede leer (git ls-remote)"
elif [ "$(id -u)" != 0 ]; then
  warn "no se pudo leer el repo remoto (correr con sudo: la credencial es root-only)"
else
  bad "no se puede leer el repo remoto: sudo bash scripts/set-github-token.sh"
fi

# ------------------------------------------------------------------
section "Venv y dependencias Python (pinneadas)"
# ------------------------------------------------------------------
if [ -x "$PY" ]; then
  ok "venv presente (.venv)"
else
  bad "venv ausente: correr sudo bash scripts/bootstrap.sh"
  PY=python3
fi
if grep -q '^include-system-site-packages = true' .venv/pyvenv.cfg 2>/dev/null; then
  ok "venv ve los paquetes de apt (system-site-packages)"
else
  bad "venv SIN system-site-packages (cv2/RPi.GPIO invisibles)"
fi

CONFIG_READABLE=1
if [ -f config-runtime.json ] && [ ! -r config-runtime.json ]; then
  CONFIG_READABLE=0
  warn "config-runtime.json no legible como $(whoami): correr con sudo para el diagnostico completo"
fi
SCANNER_TYPE="$($PY -c 'import json;print(json.load(open("config-runtime.json")).get("scannerType","none"))' 2>/dev/null || echo none)"
NEOPIXEL="$($PY -c 'import json;c=json.load(open("config-runtime.json"));print(1 if c.get("gpio",{}).get("neopixel",{}).get("enabled") else 0)' 2>/dev/null || echo 0)"
HAS_SCREEN="$($PY -c 'import json;c=json.load(open("config-runtime.json"));print(1 if c.get("screen",{}).get("hasScreen") else 0)' 2>/dev/null || echo 0)"
echo "  info  variante=$SCANNER_TYPE pantalla=$HAS_SCREEN neopixel=$NEOPIXEL"

req_files="device/requirements.txt"
[ "$SCANNER_TYPE" = opencv ] && req_files="$req_files device/requirements-opencv.txt"
[ "$NEOPIXEL" = 1 ] && req_files="$req_files device/requirements-neopixel.txt"

while IFS='|' read -r estado detalle; do
  case "$estado" in
    OK)   ok "$detalle" ;;
    FAIL) bad "$detalle" ;;
  esac
done < <($PY - $req_files <<'PYEOF'
import sys
from importlib import metadata

for req_file in sys.argv[1:]:
    for line in open(req_file):
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, want = line.split("==")
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            print(f"FAIL|{name} NO instalado (pin {want})")
            continue
        if have == want:
            print(f"OK|{name}=={have}")
        else:
            print(f"FAIL|{name}=={have} (pin {want})")
PYEOF
)

# Imports de apt (no pinneables por pip) segun variante
if $PY -c 'import RPi.GPIO' >/dev/null 2>&1; then
  ok "RPi.GPIO importable (apt)"
else
  warn "RPi.GPIO no importable (GPIO en modo simulado: normal solo en dev)"
fi
if [ "$SCANNER_TYPE" = opencv ]; then
  if $PY -c 'import cv2' >/dev/null 2>&1; then
    ok "cv2 $($PY -c 'import cv2;print(cv2.__version__)') importable (apt)"
  else
    bad "cv2 no importable: apt-get install python3-opencv"
  fi
  if $PY -c 'import pyzbar.pyzbar' >/dev/null 2>&1; then
    ok "pyzbar importable (libzbar0)"
  else
    bad "pyzbar no importable: apt libzbar0 + pip pyzbar"
  fi
  command -v uhubctl >/dev/null && ok "uhubctl presente" || bad "uhubctl ausente (recuperacion USB de camara)"
fi
if [ "$NEOPIXEL" = 1 ]; then
  $PY -c 'import neopixel' >/dev/null 2>&1 && ok "neopixel importable" || bad "neopixel no importable"
fi

# ------------------------------------------------------------------
section "Node / PM2 / nginx"
# ------------------------------------------------------------------
nodev="$(node -v 2>/dev/null || echo v0)"
[ "${nodev#v}" != "$nodev" ] && [ "$(echo "${nodev#v}" | cut -d. -f1)" -ge 18 ] \
  && ok "node $nodev (>= 18)" || bad "node $nodev (se requiere >= 18)"
command -v pm2 >/dev/null && ok "pm2 $(pm2 -v 2>/dev/null | tail -1)" || bad "pm2 ausente"
command -v nginx >/dev/null && ok "nginx presente" || warn "nginx ausente (sin proxy :80)"

# PM2 corre como root: sin sudo solo se ve la lista (vacia) del usuario
if [ "$(id -u)" = 0 ]; then
  pm2json="$(pm2 jlist 2>/dev/null | tr -d '\n')"
else
  pm2json="$(sudo -n pm2 jlist 2>/dev/null | tr -d '\n')"
fi
procs="aditum-device"
[ "$HAS_SCREEN" = 1 ] && procs="$procs aditum-web"
for proc in $procs; do
  if [ -z "$pm2json" ]; then
    warn "PM2 no consultable como $(whoami) (correr con sudo)"
    break
  elif echo "$pm2json" | grep -q "\"name\":\"$proc\".*\"status\":\"online\"\|\"status\":\"online\".*\"name\":\"$proc\""; then
    ok "PM2 $proc online"
  else
    bad "PM2 $proc NO online"
  fi
done

# ------------------------------------------------------------------
section "Configuracion"
# ------------------------------------------------------------------
if [ ! -f config-runtime.json ]; then
  warn "sin config-runtime.json (modo seguro con config-default): configurar en /admin"
elif [ "$CONFIG_READABLE" = 0 ]; then
  : # ya se aviso arriba: sin sudo no se puede validar
elif $PY -c '
import json, jsonschema
cfg = json.load(open("config-runtime.json"))
schema = json.load(open("config.schema.json"))
jsonschema.validate(cfg, schema)' 2>/dev/null; then
  ok "config-runtime.json valida contra el schema"
else
  bad "config-runtime.json NO valida contra config.schema.json"
fi
[ -s device-id.txt ] && ok "identidad: $(cat device-id.txt)" || warn "sin device-id.txt (equipo sin identidad)"
if [ -s device-token.txt ]; then
  ok "token provisionado (API con enforcement)"
else
  warn "SIN token: API abierto (modo compatibilidad) hasta provisionar"
fi

# ------------------------------------------------------------------
section "Hardware segun variante"
# ------------------------------------------------------------------
GATES="$($PY -c 'import json;c=json.load(open("config-runtime.json"));print(len(c.get("gpio",{}).get("gates",[])))' 2>/dev/null || echo 0)"
if [ "$GATES" -gt 0 ]; then
  [ -e /dev/gpiomem ] || [ -e /dev/gpiomem0 ] && ok "GPIO accesible ($GATES portones configurados)" || bad "sin /dev/gpiomem (¿no es una Raspberry?)"
fi
if [ "$SCANNER_TYPE" = opencv ]; then
  # Solo camaras USB reales (los codecs del SoC tambien exponen /dev/video*)
  cams=0
  for v in /sys/class/video4linux/video*; do
    [ -e "$v" ] || continue
    case "$(readlink -f "$v")" in *usb*) cams=$((cams+1));; esac
  done
  [ "$cams" -gt 0 ] && ok "$cams nodo(s) de camara USB presentes" || bad "sin camaras USB conectadas"
fi
if [ "$SCANNER_TYPE" = hid ]; then
  $PY - <<'PYEOF' && ok "lectores HID de la config detectados en /proc" || bad "algun lector HID de la config NO esta conectado"
import json, sys
cfg = json.load(open("config-runtime.json"))
names = [s.get("deviceName", "").lower() for s in cfg.get("scanners", []) if s.get("deviceName")]
proc = open("/proc/bus/input/devices").read().lower()
sys.exit(0 if all(n in proc for n in names) else 1)
PYEOF
fi

# ------------------------------------------------------------------
section "Servicios HTTP"
# ------------------------------------------------------------------
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8080/ || echo 000)"
[ "$code" = 200 ] && ok "API :8080 responde 200" || bad "API :8080 responde $code"
if command -v nginx >/dev/null; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost/ || echo 000)"
  [ "$code" = 200 ] && ok "nginx :80 responde 200" || bad "nginx :80 responde $code"
fi
if [ "$HAS_SCREEN" = 1 ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:3000/api/config || echo 000)"
  [ "$code" = 200 ] && ok "pantalla :3000 responde 200" || bad "pantalla :3000 responde $code"
fi

# ------------------------------------------------------------------
echo
echo "Resultado: $PASS OK, $WARN WARN, $FAIL FAIL"
[ "$FAIL" -eq 0 ] && echo "El equipo esta sano." || echo "Hay problemas: correr sudo bash scripts/bootstrap.sh suele arreglarlos."
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
