#!/usr/bin/env bash
# Instala en ESTE equipo el token de GitHub que usan el auto-update y el
# bootstrap para leer el repo cuando es privado.
#
#   sudo bash scripts/set-github-token.sh              (lo pide por teclado)
#   sudo bash scripts/set-github-token.sh <token>      (no interactivo)
#   echo <token> | sudo bash scripts/set-github-token.sh -
#
# Preferir la forma interactiva o por stdin: el token pasado como argumento
# queda en el historial del shell y en `ps`.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_URL="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || echo https://github.com/Sergiocr16/aditum-gate)"
# shellcheck source=scripts/github-auth.sh
. "$REPO_DIR/scripts/github-auth.sh"

[ "$(id -u)" = 0 ] || { echo "Correr con sudo"; exit 1; }

token="${1-}"
if [ "$token" = "-" ]; then
  IFS= read -r token || true
elif [ -z "$token" ]; then
  if { : < /dev/tty; } 2>/dev/null; then
    printf "Token de GitHub (lectura de Sergiocr16/aditum-gate): " > /dev/tty
    IFS= read -rs token < /dev/tty
    echo > /dev/tty
  fi
fi
[ -n "$token" ] || { echo "Sin token: nada que hacer"; exit 1; }

# Probar ANTES de guardar: un token vencido pisando al bueno deja el equipo
# sin actualizaciones, que es justo lo que este mecanismo evita.
if ! github_token_check "$token" "$REPO_URL"; then
  echo "FALLO: ese token no pudo leer $REPO_URL (no se cambio nada)" >&2
  echo "Revisar que tenga acceso de lectura al repo y que no haya expirado." >&2
  exit 1
fi

github_token_save "$token"
echo "Token guardado en $GH_TOKEN_FILE (root, 600)"

if github_repo_reachable "$REPO_URL"; then
  echo "OK: el repo $REPO_URL se puede leer con la credencial instalada"
else
  echo "FALLO: la credencial quedo instalada pero el repo no se pudo leer" >&2
  exit 1
fi
