#!/usr/bin/env bash
# Auto-update pull-based de aditum-gate. Las Pis estan detras de NAT, asi que
# el deploy es "la Pi se actualiza sola": lo dispara aditum-update.timer cada
# 15 min como root. Tambien es una pasada de reparacion: regenera venv y deps
# si faltan o cambiaron (stamps sha256), aunque no haya commit nuevo.
#
# Sin rollback automatico a proposito: oscila y enmascara; el fix es pushear
# una correccion (llega a toda la flota en <=15 min). Redes de seguridad:
# config-default (modo seguro), autorestart de PM2, log ruidoso aqui.
set -euo pipefail

# PATH explicito (bajo systemd el env es minimo); overrideable para tests
export PATH="${ADITUM_PATH_OVERRIDE:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="${ADITUM_BRANCH:-production}"
LOCK_FILE="${ADITUM_LOCK_FILE:-/var/lock/aditum-update.lock}"
cd "$REPO_DIR"

# Anti-solape (comparte lock con bootstrap.sh); si esta ocupado, este ciclo
# del timer simplemente se salta.
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

log() { echo "$(date '+%F %T') $*"; }

# ------------------------------------------------------------------
# 1. Codigo
# ------------------------------------------------------------------
# Self-heal: git como root exige safe.directory (bootstrap lo deja seteado,
# pero una instalacion manual no); sin esto el update falla para siempre.
git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$REPO_DIR" || \
  git config --system --add safe.directory "$REPO_DIR" 2>/dev/null || true

git fetch origin "$BRANCH" --quiet

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
UPDATED=0

if [ "$LOCAL" != "$REMOTE" ]; then
  log "Actualizando $LOCAL -> $REMOTE"
  # checkout -qf cura HEADs en un branch equivocado; nunca clean -x
  # (preserva .venv/, device-id.txt, device-token.txt, config-runtime.json)
  git checkout -qf "$BRANCH" 2>/dev/null || git checkout -qb "$BRANCH" "origin/$BRANCH"
  git reset --hard "origin/$BRANCH" --quiet
  git clean -fd --quiet
  UPDATED=1
fi

# ------------------------------------------------------------------
# 2. Dependencias por stamps (auto-reparacion: corren en cada pasada)
# ------------------------------------------------------------------
stamp() { sha256sum "$1" | cut -d' ' -f1; }

if [ ! -x .venv/bin/python3 ]; then
  log "Venv ausente: recreando"
  python3 -m venv --system-site-packages .venv
fi
# El venv debe ver los paquetes de apt (python3-opencv, RPi.GPIO): un venv
# creado a mano sin --system-site-packages deja imports rotos para siempre.
if grep -q '^include-system-site-packages = false' .venv/pyvenv.cfg 2>/dev/null; then
  log "Venv sin system-site-packages: habilitando"
  sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' .venv/pyvenv.cfg
fi
# El stamp cubre TODOS los requirements (nucleo + extras por variante):
# cambiar cualquier version pinneada hace que la flota la reinstale sola.
SCANNER_TYPE="$(.venv/bin/python3 -c 'import json;print(json.load(open("config-runtime.json")).get("scannerType",""))' 2>/dev/null || echo '')"
NEOPIXEL="$(.venv/bin/python3 -c 'import json;c=json.load(open("config-runtime.json"));print(1 if c.get("gpio",{}).get("neopixel",{}).get("enabled") else 0)' 2>/dev/null || echo 0)"

REQ_STAMP="$(cat device/requirements*.txt | sha256sum | cut -d' ' -f1)"
if [ "$REQ_STAMP" != "$(cat .venv/.requirements.sha256 2>/dev/null || true)" ]; then
  log "requirements cambiaron: pip install pinneado"
  .venv/bin/pip install -q -r device/requirements.txt
  if [ "$SCANNER_TYPE" = opencv ]; then
    .venv/bin/pip install -q -r device/requirements-opencv.txt
  fi
  if [ "$NEOPIXEL" = 1 ]; then
    .venv/bin/pip install -q -r device/requirements-neopixel.txt
  fi
  echo "$REQ_STAMP" > .venv/.requirements.sha256
fi

WEB_STAMP="$(stamp web/package-lock.json)"
if [ "$WEB_STAMP" != "$(cat web/node_modules/.aditum-stamp 2>/dev/null || true)" ]; then
  log "package-lock.json cambio: npm install"
  npm install --prefix web --omit=dev --silent
  echo "$WEB_STAMP" > web/node_modules/.aditum-stamp
fi

# Extras por variante (espejo de bootstrap install_variant_extras): la config
# puede cambiar a opencv/neopixel DESPUES de instalar; sin esto el proceso
# queda en crash-loop por import faltante hasta que alguien corra bootstrap.
if [ "$SCANNER_TYPE" = opencv ] && ! .venv/bin/python3 -c 'import cv2, pyzbar' >/dev/null 2>&1; then
  log "Variante opencv sin dependencias: instalando extras (apt + pip pinneado)"
  apt-get install -y -qq python3-opencv libzbar0 uhubctl
  .venv/bin/pip install -q -r device/requirements-opencv.txt
fi
if [ "$NEOPIXEL" = 1 ] && ! .venv/bin/python3 -c 'import neopixel' >/dev/null 2>&1; then
  log "NeoPixel habilitado sin dependencias: instalando extras (pip pinneado)"
  .venv/bin/pip install -q -r device/requirements-neopixel.txt
fi

# ------------------------------------------------------------------
# 3. Units systemd (si el repo trae una version nueva)
# ------------------------------------------------------------------
UNITS_CHANGED=0
for unit in aditum-update.service aditum-update.timer; do
  if ! cmp -s "scripts/systemd/$unit" "/etc/systemd/system/$unit"; then
    cp "scripts/systemd/$unit" "/etc/systemd/system/$unit"
    UNITS_CHANGED=1
  fi
done
if [ "$UNITS_CHANGED" = 1 ]; then
  log "Units systemd actualizadas"
  systemctl daemon-reload
  systemctl reenable aditum-update.timer >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------------
# 4. Reinicio + health check (solo si hubo update de codigo)
# ------------------------------------------------------------------
if [ "$UPDATED" = 1 ]; then
  pm2 startOrRestart ecosystem.config.js --update-env >/dev/null
  pm2 save >/dev/null
  sleep 8
  if curl -fsS -m 5 http://localhost:8080/ >/dev/null; then
    log "Actualizacion aplicada y health OK ($REMOTE)"
  else
    log "HEALTHCHECK FAILED tras actualizar a $REMOTE — revisar 'pm2 logs aditum-device'"
    exit 1
  fi
fi
