#!/usr/bin/env bash
# ============================================================================
# Instalador de un comando de aditum-gate para Raspberry Pi.
#
#   curl -fsSL https://raw.githubusercontent.com/Sergiocr16/aditum-gate/main/scripts/bootstrap.sh | sudo bash
#
# Funciona en una Pi virgen Y sobre una instalacion vieja (cualquier branch
# historico): respalda todo en ~/aditum-backup-<fecha>/, cosecha los valores
# hardcodeados viejos como sugerencias para el wizard, limpia lo legacy
# (pm2 del usuario pi, crons de reinicio, sudoers, nginx viejo) e instala el
# sistema nuevo (Node 20 system-wide, venv Python, PM2 root, auto-update).
#
# Idempotente: re-correrlo es seguro y sirve de reparador.
#
# Variables opcionales:
#   ADITUM_HOME=/home/pi/aditum-gate   ruta de instalacion
#   ADITUM_USER=pi                     usuario de la instalacion vieja
#   ADITUM_BRANCH=main                 branch a trackear
#   ADITUM_RECONFIGURE=1               forzar el wizard aunque haya config
#   ADITUM_CONFIG_URL=... / ADITUM_CONFIG_FILE=...   config preparada
#   ADITUM_NONINTERACTIVE=1 + ADITUM_DEVICE_ID etc.  (ver configure.py)
# ============================================================================
set -euo pipefail

REPO_URL="https://github.com/Sergiocr16/aditum-gate"
REPO_DIR="${ADITUM_HOME:-/home/pi/aditum-gate}"
PI_USER="${ADITUM_USER:-pi}"
BRANCH="${ADITUM_BRANCH:-main}"
BACKUP_DIR="/home/$PI_USER/aditum-backup-$(date +%Y%m%d-%H%M%S)"
LOCK_FILE="${ADITUM_LOCK_FILE:-/var/lock/aditum-update.lock}"
LOG_FILE=/var/log/aditum-bootstrap.log

log()  { echo -e "\n==> $*"; }
warn() { echo "AVISO: $*" >&2; }

# ----------------------------------------------------------------------------
preflight() {
  [ "$(id -u)" = 0 ] || { echo "Correr con sudo: curl ... | sudo bash"; exit 1; }

  case "$(uname -m)" in
    armv6l)
      echo "Hardware no soportado (Pi Zero/1, armv6): se requiere Pi 3 o superior."
      exit 1 ;;
  esac

  exec > >(tee -a "$LOG_FILE") 2>&1
  log "Bootstrap aditum-gate $(date '+%F %T') — repo: $REPO_DIR branch: $BRANCH"

  # Lock compartido con self-update (espera hasta 120s a que termine un ciclo)
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  flock -w 120 9 || { echo "Otro update en curso; reintentar luego"; exit 1; }

  git config --system --add safe.directory "$REPO_DIR" 2>/dev/null || true
}

# ----------------------------------------------------------------------------
backup_and_harvest() {
  local has_old=0
  [ -d "$REPO_DIR" ] && has_old=1
  crontab -u "$PI_USER" -l 2>/dev/null | grep -qE 'pm2|nginx|aditum' && has_old=1
  [ -f /etc/systemd/system/pm2-$PI_USER.service ] && has_old=1
  [ "$has_old" = 1 ] || { log "Pi virgen: no hay nada que respaldar"; return 0; }

  log "Instalacion previa detectada: respaldando en $BACKUP_DIR"
  mkdir -p "$BACKUP_DIR/old-app"

  crontab -u "$PI_USER" -l > "$BACKUP_DIR/crontab-$PI_USER.txt" 2>/dev/null || true
  crontab -l > "$BACKUP_DIR/crontab-root.txt" 2>/dev/null || true
  cp -a /etc/sudoers.d/nginx-restart "$BACKUP_DIR/" 2>/dev/null || true
  cp -a /etc/nginx/sites-available "$BACKUP_DIR/nginx-sites-available" 2>/dev/null || true

  # PM2 viejo del usuario pi (nvm no se carga con su -, ir al binario directo)
  local pm2_bin
  pm2_bin="$(ls /home/$PI_USER/.nvm/versions/node/*/bin/pm2 2>/dev/null | sort -V | tail -1 || true)"
  [ -n "$pm2_bin" ] && su "$PI_USER" -c "'$pm2_bin' jlist" > "$BACKUP_DIR/pm2-jlist.json" 2>/dev/null || true

  if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" branch --show-current > "$BACKUP_DIR/git-branch.txt" 2>/dev/null || true
    git -C "$REPO_DIR" status > "$BACKUP_DIR/git-status.txt" 2>/dev/null || true
    git -C "$REPO_DIR" diff > "$BACKUP_DIR/working-tree.diff" 2>/dev/null || true
  fi

  # Cosecha: scripts viejos con config hardcodeada -> hints para el wizard
  for f in scannerQr.py scannerQrExit.py scanner.py scannerExit.py serverGPIO.py index.js; do
    cp -a "$REPO_DIR/$f" "$BACKUP_DIR/old-app/" 2>/dev/null || true
  done
  # Identidad/config nuevas si ya existian (ademas sobreviven al reset)
  for f in device-id.txt device-token.txt config-runtime.json hikvision-cards.json; do
    cp -a "$REPO_DIR/$f" "$BACKUP_DIR/" 2>/dev/null || true
  done
  chown -R "$PI_USER:$PI_USER" "$BACKUP_DIR" 2>/dev/null || true
}

# ----------------------------------------------------------------------------
teardown_legacy() {
  log "Limpiando instalacion vieja (respaldo en $BACKUP_DIR)"

  # PM2 del usuario pi + su arranque systemd
  local pm2_bin
  pm2_bin="$(ls /home/$PI_USER/.nvm/versions/node/*/bin/pm2 2>/dev/null | sort -V | tail -1 || true)"
  if [ -n "$pm2_bin" ]; then
    su "$PI_USER" -c "'$pm2_bin' delete all; '$pm2_bin' save --force; '$pm2_bin' kill" 2>/dev/null || true
  fi
  systemctl disable --now "pm2-$PI_USER" 2>/dev/null || true
  rm -f "/etc/systemd/system/pm2-$PI_USER.service"
  systemctl daemon-reload

  # Procesos huerfanos de la instalacion vieja
  pkill -u "$PI_USER" -f 'PM2|index\.js|ng serve' 2>/dev/null || true
  pkill -f 'scannerQr(Exit)?\.py|scanner(Exit)?\.py|serverGPIO\.py|led\.py' 2>/dev/null || true

  # Crontabs: quitar SOLO las lineas de reinicios/aditum (quirurgico)
  for u in "$PI_USER" root; do
    if crontab -u "$u" -l >/dev/null 2>&1; then
      local filtered
      filtered="$(crontab -u "$u" -l | grep -vE 'pm2|nginx|aditum|/sbin/reboot' || true)"
      if [ -n "$(echo "$filtered" | tr -d '[:space:]')" ]; then
        echo "$filtered" | crontab -u "$u" -
      else
        crontab -u "$u" -r 2>/dev/null || true
      fi
    fi
  done

  rm -f /etc/sudoers.d/nginx-restart

  # Desactivar sites nginx viejos (se regenera el nuestro en configure_nginx)
  rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/express-aditum-gate
  # NUNCA tocar remoteiot / VNC / configuracion de red.
}

# ----------------------------------------------------------------------------
sync_repo() {
  if [ -d "$REPO_DIR/.git" ] && git -C "$REPO_DIR" remote get-url origin 2>/dev/null | grep -q 'aditum-gate'; then
    log "Actualizando repo existente a origin/$BRANCH"
    git -C "$REPO_DIR" fetch origin "$BRANCH"
    git -C "$REPO_DIR" checkout -qf "$BRANCH" 2>/dev/null || \
      git -C "$REPO_DIR" checkout -qb "$BRANCH" "origin/$BRANCH"
    git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
    # sin -x: conserva device-id/token, config-runtime, .venv, hikvision-cards
    git -C "$REPO_DIR" clean -fd
  else
    if [ -d "$REPO_DIR" ]; then
      warn "El directorio existe pero no es el repo esperado: se mueve al backup"
      mkdir -p "$BACKUP_DIR"
      mv "$REPO_DIR" "$BACKUP_DIR/repo-viejo"
    fi
    log "Clonando $REPO_URL"
    git clone -b "$BRANCH" "$REPO_URL" "$REPO_DIR"
  fi
}

# ----------------------------------------------------------------------------
install_base_packages() {
  log "Paquetes base (apt)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git curl ca-certificates python3 python3-pip \
    python3-venv python3-dev build-essential
}

install_node_pm2() {
  if ! command -v node >/dev/null || [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 18 ]; then
    log "Instalando Node 20 system-wide (NodeSource)"
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y -qq nodejs
  else
    log "Node $(node -v) ya disponible para root"
  fi
  command -v pm2 >/dev/null || { log "Instalando PM2"; npm install -g pm2 --silent; }
  pm2 install pm2-logrotate --silent >/dev/null 2>&1 || true
  pm2 set pm2-logrotate:max_size 10M >/dev/null 2>&1 || true
  pm2 set pm2-logrotate:retain 7 >/dev/null 2>&1 || true
}

setup_venv() {
  cd "$REPO_DIR"
  [ -x .venv/bin/python3 ] || { log "Creando venv Python"; python3 -m venv --system-site-packages .venv; }
  local stamp
  stamp="$(sha256sum device/requirements.txt | cut -d' ' -f1)"
  if [ "$stamp" != "$(cat .venv/.requirements.sha256 2>/dev/null || true)" ]; then
    log "Instalando dependencias Python"
    .venv/bin/pip install -q -r device/requirements.txt
    echo "$stamp" > .venv/.requirements.sha256
  fi
}

setup_web_deps() {
  cd "$REPO_DIR"
  local stamp
  stamp="$(sha256sum web/package-lock.json | cut -d' ' -f1)"
  if [ "$stamp" != "$(cat web/node_modules/.aditum-stamp 2>/dev/null || true)" ]; then
    log "Instalando dependencias Node del servidor de pantalla"
    npm install --prefix web --omit=dev --silent
    echo "$stamp" > web/node_modules/.aditum-stamp
  fi
}

# ----------------------------------------------------------------------------
run_configurator() {
  cd "$REPO_DIR"

  if [ -f config-runtime.json ] && [ "${ADITUM_RECONFIGURE:-0}" != 1 ]; then
    local current_id
    current_id="$(.venv/bin/python3 -c 'import json;print(json.load(open("config-runtime.json")).get("deviceId",""))' 2>/dev/null || true)"
    if [ "${ADITUM_NONINTERACTIVE:-0}" = 1 ] || [ ! -r /dev/tty ]; then
      log "Config existente de '$current_id' conservada (ADITUM_RECONFIGURE=1 para regenerar)"
      return 0
    fi
    local answer
    read -r -p "Ya hay configuracion para '$current_id'. ¿Conservarla? [S/n] " answer </dev/tty || answer=""
    case "$answer" in n|N|no|NO) ;; *) log "Configuracion conservada"; return 0 ;; esac
  fi

  if [ -n "${ADITUM_CONFIG_URL:-}" ]; then
    log "Descargando config de $ADITUM_CONFIG_URL"
    curl -fsSL "$ADITUM_CONFIG_URL" -o /tmp/aditum-config.json
    .venv/bin/python3 scripts/configure.py --from-file /tmp/aditum-config.json
  elif [ -n "${ADITUM_CONFIG_FILE:-}" ]; then
    .venv/bin/python3 scripts/configure.py --from-file "$ADITUM_CONFIG_FILE"
  elif [ "${ADITUM_NONINTERACTIVE:-0}" = 1 ]; then
    .venv/bin/python3 scripts/configure.py --non-interactive
  else
    local hints=""
    [ -d "$BACKUP_DIR/old-app" ] && hints="--hints $BACKUP_DIR/old-app"
    # shellcheck disable=SC2086
    .venv/bin/python3 scripts/configure.py $hints
  fi
}

install_variant_extras() {
  cd "$REPO_DIR"
  local scanner_type neopixel
  scanner_type="$(.venv/bin/python3 -c 'import json;print(json.load(open("config-runtime.json")).get("scannerType","none"))' 2>/dev/null || echo none)"
  neopixel="$(.venv/bin/python3 -c 'import json;c=json.load(open("config-runtime.json"));print(1 if c.get("gpio",{}).get("neopixel",{}).get("enabled") else 0)' 2>/dev/null || echo 0)"

  if [ "$scanner_type" = opencv ]; then
    log "Extras OpenCV (apt python3-opencv + pyzbar)"
    apt-get install -y -qq python3-opencv libzbar0 uhubctl
    .venv/bin/pip install -q pyzbar
  fi
  if [ "$neopixel" = 1 ]; then
    log "Extras NeoPixel"
    .venv/bin/pip install -q rpi_ws281x adafruit-circuitpython-neopixel adafruit-blinka
  fi
}

configure_nginx() {
  cd "$REPO_DIR"
  command -v nginx >/dev/null || { log "Instalando nginx"; apt-get install -y -qq nginx; }

  local has_screen port
  has_screen="$(.venv/bin/python3 -c 'import json;c=json.load(open("config-runtime.json"));print(1 if c.get("screen",{}).get("hasScreen") else 0)' 2>/dev/null || echo 0)"
  port=8080; [ "$has_screen" = 1 ] && port=3000
  log "nginx :80 -> localhost:$port"

  sed "s/__UPSTREAM_PORT__/$port/" scripts/nginx/express-aditum-gate.conf.template \
    > /etc/nginx/sites-available/express-aditum-gate
  ln -sf /etc/nginx/sites-available/express-aditum-gate /etc/nginx/sites-enabled/express-aditum-gate
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl enable --now nginx && systemctl reload nginx
}

start_pm2() {
  cd "$REPO_DIR"
  log "Arrancando procesos con PM2 (root)"
  pm2 startOrRestart ecosystem.config.js --update-env
  pm2 save
  pm2 startup systemd -u root --hp /root >/dev/null 2>&1 || true
  systemctl enable pm2-root 2>/dev/null || true
}

install_update_timer() {
  cd "$REPO_DIR"
  log "Habilitando auto-update (systemd timer cada 15 min)"
  if [ "$REPO_DIR" != /home/pi/aditum-gate ]; then
    sed "s|/home/pi/aditum-gate|$REPO_DIR|g" scripts/systemd/aditum-update.service \
      > /etc/systemd/system/aditum-update.service
  else
    cp scripts/systemd/aditum-update.service /etc/systemd/system/
  fi
  cp scripts/systemd/aditum-update.timer /etc/systemd/system/
  [ "$BRANCH" != main ] && echo "ADITUM_BRANCH=$BRANCH" > /etc/default/aditum-gate
  systemctl daemon-reload
  systemctl enable --now aditum-update.timer

  cat > /etc/logrotate.d/aditum-update <<'EOF'
/var/log/aditum-update.log /var/log/aditum-bootstrap.log {
  size 5M
  rotate 4
  compress
  missingok
  notifempty
}
EOF
}

# ----------------------------------------------------------------------------
verify_and_summarize() {
  cd "$REPO_DIR"
  sleep 8
  local health=FALLO has_screen device_id
  curl -fsS -m 5 http://localhost:8080/ >/dev/null 2>&1 && health=OK
  device_id="$(cat device-id.txt 2>/dev/null || echo 'sin provisionar')"
  has_screen="$(.venv/bin/python3 -c 'import json;c=json.load(open("config-runtime.json"));print(1 if c.get("screen",{}).get("hasScreen") else 0)' 2>/dev/null || echo 0)"

  echo
  echo "============================================================"
  echo " Instalacion terminada"
  echo "   Dispositivo : $device_id"
  echo "   Health :8080: $health"
  [ "$has_screen" = 1 ] && echo "   Pantalla    : http://localhost:3000 (verificar por VNC)"
  [ -d "$BACKUP_DIR" ] && echo "   Respaldo    : $BACKUP_DIR"
  echo
  echo " Pendientes:"
  echo "   - Provisionar token si falta: PUT /token o device-token.txt (ver docs/API.md)"
  echo "   - Registrar el Entry Point en Aditum con la URL del tunel remoteiot"
  echo "   - Editor local de config: http://localhost:8080/admin"
  echo "============================================================"
  pm2 status || true
  if [ "$health" != OK ]; then
    warn "El health check fallo: revisar 'sudo pm2 logs aditum-device'"
  fi
}

# ----------------------------------------------------------------------------
main() {
  preflight
  backup_and_harvest
  teardown_legacy
  install_base_packages
  sync_repo
  install_node_pm2
  setup_venv
  setup_web_deps
  run_configurator
  install_variant_extras
  configure_nginx
  start_pm2
  install_update_timer
  verify_and_summarize
}

# Todo el script esta en funciones: bash lo parsea completo antes de ejecutar
# (necesario al correr via curl | bash). La interactividad va por /dev/tty.
if [ -t 0 ]; then
  main "$@"
elif [ -r /dev/tty ]; then
  main "$@" </dev/tty
else
  ADITUM_NONINTERACTIVE=1 main "$@"
fi
