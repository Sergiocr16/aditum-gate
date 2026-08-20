#!/usr/bin/env bash
# Credenciales de GitHub para clonar y actualizar cuando el repo es privado.
#
# NO es ejecutable por si solo: lo sourcean bootstrap.sh, self-update.sh y
# doctor.sh. El token NUNCA vive en el repo (seria publico en el historial
# y GitHub revoca solo los que detecta): vive en /etc/aditum-gate/, root
# 600, fuera del arbol de git para que `git clean -fd` no lo borre.
#
# Se registra como credential helper a nivel --system porque quien clona y
# actualiza es root (bootstrap y el timer de auto-update).

GH_TOKEN_FILE="${ADITUM_GH_TOKEN_FILE:-/etc/aditum-gate/github-token}"
GH_CRED_FILE="${ADITUM_GH_CRED_FILE:-/etc/aditum-gate/git-credentials}"
GH_CRED_USER=x-access-token
# Siempre --system en el equipo (quien clona y actualiza es root); la
# variable existe para poder probar la cadena completa sin ser root.
GH_CONFIG_SCOPE="${ADITUM_GIT_CONFIG_SCOPE:---system}"

github_token_present() { [ -s "$GH_TOKEN_FILE" ]; }

github_token_read() { tr -d ' \t\n\r' < "$GH_TOKEN_FILE" 2>/dev/null; }

# Deja git listo para autenticarse contra github.com con el token guardado.
# Idempotente y barato: se llama en cada pasada del self-update (self-heal
# de una config borrada a mano o de un git reinstalado).
github_credentials_apply() {
  github_token_present || return 0
  local token
  token="$(github_token_read)"
  [ -n "$token" ] || return 0
  install -d -m 700 "$(dirname "$GH_CRED_FILE")"
  ( umask 077; printf 'https://%s:%s@github.com\n' "$GH_CRED_USER" "$token" > "$GH_CRED_FILE" )
  chmod 600 "$GH_CRED_FILE"
  git config "$GH_CONFIG_SCOPE" --replace-all "credential.https://github.com.helper" \
    "store --file=$GH_CRED_FILE" 2>/dev/null || return 1
  git config "$GH_CONFIG_SCOPE" --replace-all "credential.https://github.com.username" \
    "$GH_CRED_USER" 2>/dev/null || return 1
}

# Guarda un token nuevo (y aplica la config). Solo root.
github_token_save() {
  local token="$1"
  [ -n "$token" ] || return 1
  install -d -m 700 "$(dirname "$GH_TOKEN_FILE")"
  ( umask 077; printf '%s\n' "$token" > "$GH_TOKEN_FILE" )
  chmod 600 "$GH_TOKEN_FILE"
  github_credentials_apply
}

# ¿Se puede leer el repo con lo que hay configurado ahora?
github_repo_reachable() {
  local url="$1"
  GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true \
    git ls-remote --exit-code "$url" HEAD >/dev/null 2>&1
}

# Pide el token por teclado. Vale incluso bajo `curl ... | sudo bash`,
# porque lee de /dev/tty y no del stdin (que es el script). Sin TTY
# (systemd, ssh no interactivo) devuelve 1 sin colgarse.
github_token_prompt() {
  # Abrir /dev/tty de verdad: el nodo existe aunque no haya terminal de
  # control (systemd, ssh no interactivo) y ahi no se puede preguntar.
  { : < /dev/tty; } 2>/dev/null || return 1
  local token=""
  {
    echo
    echo "El repo de aditum-gate es privado: se necesita un token de GitHub"
    echo "con permiso de LECTURA de contenido sobre Sergiocr16/aditum-gate."
    printf "Token (no se muestra al escribir, Enter para omitir): "
  } > /dev/tty
  IFS= read -rs token < /dev/tty || return 1
  echo > /dev/tty
  [ -n "$token" ] || return 1
  github_token_save "$token"
}
