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
BRANCH="${ADITUM_BRANCH:-main}"
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
REQ_STAMP="$(stamp device/requirements.txt)"
if [ "$REQ_STAMP" != "$(cat .venv/.requirements.sha256 2>/dev/null || true)" ]; then
  log "requirements.txt cambio: pip install"
  .venv/bin/pip install -q -r device/requirements.txt
  echo "$REQ_STAMP" > .venv/.requirements.sha256
fi

WEB_STAMP="$(stamp web/package-lock.json)"
if [ "$WEB_STAMP" != "$(cat web/node_modules/.aditum-stamp 2>/dev/null || true)" ]; then
  log "package-lock.json cambio: npm install"
  npm install --prefix web --omit=dev --silent
  echo "$WEB_STAMP" > web/node_modules/.aditum-stamp
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
