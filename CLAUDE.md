# CLAUDE.md — aditum-gate

Control de acceso para condominios sobre Raspberry Pi. Este archivo documenta
las reglas y la arquitectura del proyecto para sesiones de Claude Code.

## Regla de oro: un solo branch, variantes por configuración

**Nunca crear branches por variante de producto ni duplicar archivos por
tipo de instalación.** Históricamente cada variante vivía en un branch
(`hikvision-qr`, `pistolaqr`, `qr-readers`, `qr-small`, `pedestal-app`) con
código duplicado y divergente; eso se consolidó en `main` en 2026-06. Qué hace
cada dispositivo lo decide su **config JSON** (ver `config.schema.json`):

- `scannerType`: `"hid"` (pistolas QR evdev) | `"opencv"` (cámaras USB) |
  `"hikvision"` (terminales DS-K1T323, sin scanner local) | `"none"` (solo
  apertura de portones).
- `scanners[]`: 0–2 lectores, cada uno con `role` (entry/exit), `doorId`, y
  `deviceName` (HID) o `cameraIndex` (OpenCV).
- `screen.hasScreen`, `gpio.*` (pines, watchdog, neopixel), `api.baseUrl`
  (app vs caseta.aditumcr.com) y `api.verifierStyle` (secure/legacy).

Si una funcionalidad nueva aplica solo a algunas instalaciones, se agrega un
flag al schema + `settings.py`, no un branch ni un archivo paralelo.

## Arquitectura

Dos procesos supervisados por PM2 (`ecosystem.config.js`); no hay otro
supervisor ni cron de reinicio:

1. **`aditum-device`** = `device/main.py` — un único proceso Python con
   threads: Flask :8080 (portones GPIO, Hikvision ISAPI, health), 0–2 threads
   de scanner, config-agent, watchdog, limpieza nocturna Hikvision. Ante
   fallo irrecuperable se hace `os._exit(1)` y PM2 lo relanza — no inventar
   supervisores internos.
2. **`aditum-web`** = `web/server.js` — Express+WebSocket :3000: sirve el
   build de Angular (`web/pedestal-app/dist/pedestal-app/browser`), recibe
   los estados de los scanners y los broadcastea a la pantalla, y expone
   `GET /api/config` (subset seguro de la config).

### Sistema de configuración (lo más importante)

- La config de cada Pi se administra **desde Aditum por push**: el backend
  hace `PUT /config` al Entry Point del Pi. También hay un editor local en
  `http://localhost:8080/admin` (misma vía: GET/PUT /config). El poller pull
  (`config_agent.ConfigAgent`) es solo respaldo, apagado por default
  (`polling.enabled: false`).
- Ambas vías convergen en `config_agent.apply_config()` (lock, idempotencia
  por `configRevision`, validación contra `config.schema.json`, escritura
  atómica). `apply_config` **no reinicia**: el caller decide (el handler HTTP
  responde primero y agenda `restart_process` con un Timer). Restart-on-change,
  no hot-reload: evdev/VideoCapture/GPIO no se reconfiguran en caliente.
- Todo lo demás lee `config-runtime.json` del disco: Python via
  `settings.load_settings()` una vez al boot; `web/server.js` en cada request;
  Angular via `GET /api/config`.
- Sin red → cache; sin cache → `config-default.json` (modo seguro). Una cache
  buena nunca se pisa con la default.

### Seguridad del API (Flask :8080)

- **Todos los endpoints exigen el token del dispositivo** (`Authorization:
  Bearer` o `X-Device-Token`), enforced por un `before_request` global en
  `auth.py` — fail-secure: un endpoint nuevo nace protegido. Lo público es la
  allowlist `PUBLIC_PATHS` en `auth.py`; **agregar un path ahí es una decisión
  de seguridad** que debe justificarse en el review.
- Pi sin provisionar (sin `device-token.txt`): API abierta en modo transición
  con log de error rate-limited; `GET /status` lo reporta (`provisioned`).
  No convertir esto en fail-closed: brickearía la flota.
- El contrato del API vive en `docs/API.md` — **todo endpoint nuevo o cambio
  de shape se documenta ahí** (es lo que implementa el equipo de aditum-jh).
- No agregar bypass por IP loopback: el túnel remoteiot entrega las requests
  remotas como 127.0.0.1, un bypass local abriría el API al túnel entero.

## Reglas del proyecto

- **Cero configuración hardcodeada.** doorId, doorType, hosts de API, pines,
  nombres de lectores, índices de cámara, logos: todo sale de la config. Si
  un PR agrega un literal de estos en el código, está mal.
- **Cero credenciales en el repo.** `device-id.txt`, `device-token.txt`,
  `config-runtime.json` y `hikvision-cards.json` están gitignoreados. Las
  credenciales de terminales Hikvision NO van en la config: llegan en el
  payload de `POST /update-card` desde el backend.
- **La pantalla nunca bloquea el acceso**: `screen.py` usa timeout 1s, sin
  reintentos, y es no-op si `hasScreen=false`. Mantener esa propiedad.
- **Logging, no print.** Loggers `aditum.*` definidos en `log.py`; PM2 captura
  stdout. HTTP siempre via `httpclient.py` (reintentos + timeouts) — la única
  excepción es `screen.py`, que deliberadamente no reintenta.
- **La lógica de scanning vive una sola vez** en `scanners/base.py` (prefijo,
  debounce, verify). Los subtipos solo implementan `read_code()`. No volver a
  duplicar scanners por puerta: entry/exit son dos instancias.
- Código y comentarios en **español** (sin tildes en comentarios Python por
  consistencia con el código existente); identificadores en inglés.
- Esto corre en hardware modesto mantenido por un equipo pequeño: preferir
  soluciones simples, sin frameworks nuevos ni abstracciones especulativas.

## Verificación antes de dar algo por terminado

```bash
python3 -m compileall -q device scripts/validate_configs.py scripts/configure.py
node --check web/server.js && node --check ecosystem.config.js
bash -n scripts/bootstrap.sh && bash -n scripts/self-update.sh
python3 scripts/validate_configs.py                  # configs vs schema (pip install jsonschema)
```

**El build de Angular está commiteado** (`web/pedestal-app/dist/`) porque las
Pis no compilan: todo cambio bajo `web/pedestal-app/src` exige regenerar el
build (`npm ci && npm run build` con Node 20) y commitear `dist/` en el MISMO
commit — el job `screen-build-check` del CI falla si no.

En dev (sin Raspberry) el GPIO corre en modo simulado (`RPi.GPIO` ausente) y
se puede probar el Flask con `app.test_client()`. Los cambios al schema deben
actualizar también `config-default.json`, los `examples/` y, si aplica,
`settings.py` y el subset de `GET /api/config` en `web/server.js`.

## Instalación y despliegue

- **Instalación = `scripts/bootstrap.sh`** (one-liner curl | sudo bash),
  idempotente y retroactivo: respalda y desmonta instalaciones viejas, deja
  Node 20 system-wide, venv `.venv/` (PM2 apunta a `.venv/bin/python3` — no
  cambiar a python3 de sistema), PM2 root y el timer de auto-update. El wizard
  `scripts/configure.py` genera la config desde las plantillas de `examples/`.
- **Pull-based**: cada Pi corre `scripts/self-update.sh` via systemd timer
  cada 15 min (flock compartido con bootstrap, stamps sha256 para reinstalar
  deps solo si cambian, health check final; nunca `git clean -x` — borraría
  identidad y venv). No hay push por SSH — las Pis están detrás de NAT. No
  reintroducir workflows de deploy por SSH (el branch `auto` lo intentó y no
  funciona).
- GitHub Actions (`.github/workflows/ci.yml`) solo valida: sintaxis + schema.
- Cambiar el comportamiento de un dispositivo en producción = editar su
  config en el backend (sube `configRevision`), no tocar código.

## Contexto externo

- Backend: **aditum-jh** (Java/JHipster en Heroku, repo aparte). Endpoints
  que este código consume: `aditum-gate-verifier-{entry,exit}[-secure]` y el
  de config (`gate-devices/{id}/config`, spec en el README).
- La guía de hardware Hikvision (DS-K1T323: IP estática, HTTP Listening,
  tokens) está en el vault de Obsidian: nota "Integración Hikvision
  DS-K1T323" en `10 - Projects/aditum-jh/Notes/`.
- Los branches viejos (`hikvision-qr`, `pistolaqr`, etc.) son solo referencia
  histórica; no portar código desde ellos sin pasarlo por esta arquitectura.
