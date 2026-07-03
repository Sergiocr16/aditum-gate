# aditum-gate

Control de acceso para condominios sobre Raspberry Pi: abre portones por GPIO,
valida códigos QR contra el backend de Aditum y opcionalmente muestra una
pantalla pedestal.

**Todas las variantes corren con este mismo código (branch `production`).** Qué hace
cada dispositivo lo decide su **configuración**, no el branch:

| Variante | `scannerType` | Hardware |
|---|---|---|
| Lectores QR pistola (1 o 2) | `hid` | Lector HID (ej. Newtologic 4010E / MJ-8200) |
| Cámaras USB + OpenCV (legacy) | `opencv` | Cámara(s) USB |
| Terminales Hikvision | `hikvision` | DS-K1T323 (el Pi registra tarjetas vía ISAPI) |
| Solo apertura de portones | `none` | Solo relays GPIO |

Cada una puede combinarse con: pantalla pedestal (`screen.hasScreen`), 1 o 2
lectores/cámaras (`scanners`), watchdog de red, NeoPixel, y API
`app.aditumcr.com` o `caseta.aditumcr.com` con estilo `secure` o `legacy`.

## Arquitectura

```
┌──────────────────────────── Raspberry Pi ───────────────────────────┐
│  aditum-device (Python, PM2)            aditum-web (Node, PM2)      │
│  ├─ Flask :8080  ← Aditum llama         ├─ Express+WS :3000         │
│  │   /openGate /update-card ...         │   sirve pedestal-app      │
│  ├─ editor /admin + GET /health         ├─ supervisa kiosk chromium │
│  ├─ scanners (hid/opencv) ──────────────┼─► estados de pantalla     │
│  ├─ hikvision (ISAPI → terminales)      └─ GET /api/config          │
│  ├─ config-agent ──► config-runtime.json (cache de la config remota)│
│  └─ watchdog / neopixel                                             │
└─────────────────────────────────────────────────────────────────────┘
         ▲ GET /api/gate-devices/{deviceId}/config (poller de respaldo)
   Backend Aditum (app/caseta.aditumcr.com)
```

Si la config dice `screen.hasScreen`, `aditum-web` además supervisa el
navegador kiosk (chromium a pantalla completa como el usuario de la sesión
gráfica): lo abre, lo relanza si muere y lo cierra si la pantalla se apaga
por config — revisa cada 15 s.

## Estructura del repositorio

```
aditum-gate/
├── ecosystem.config.js          # Procesos PM2: aditum-device + aditum-web
├── config.schema.json           # JSON Schema del documento de configuración
├── config-default.json          # Config "safe" de fallback (solo portones)
├── examples/                    # Una config de ejemplo por variante
├── device/                      # Código Python del Pi (UN proceso con threads)
│   ├── main.py                  # Entrypoint: arma todo según la config
│   ├── requirements.txt
│   └── aditum_gate/
│       ├── settings.py          # Carga y acceso tipado a la config
│       ├── auth.py              # Token por dispositivo (before_request fail-secure)
│       ├── admin_auth.py        # Login de persona para el editor /admin (sesion cookie)
│       ├── health.py            # GET /health: USB, lectores, PM2, sistema
│       ├── config_agent.py      # apply_config (push/pull) + poller opcional
│       ├── static/admin.html    # Editor en linea: http://localhost:8080/admin
│       ├── log.py               # Logging a stdout (PM2 lo captura)
│       ├── httpclient.py        # Session con reintentos y timeouts
│       ├── backend.py           # Verificación QR (estilos secure/legacy)
│       ├── api.py               # Flask :8080 (portones, Hikvision, health)
│       ├── gpio_relays.py       # Relays GPIO (pines desde config)
│       ├── hikvision.py         # ISAPI + CardStore persistente + limpieza 2AM
│       ├── screen.py            # Cliente de la pantalla (no-op sin pantalla)
│       ├── watchdog.py          # Reboot si se pierde la red
│       ├── leds.py              # NeoPixel opcional
│       └── scanners/
│           ├── base.py          # Lógica común: prefijo, debounce, verify
│           ├── hid.py           # Lectores pistola (evdev), 1 o 2
│           └── opencv.py        # Cámaras USB (cv2+pyzbar), 1 o 2
├── web/
│   ├── server.js                # Express+WS :3000, sirve Angular, GET /api/config, kiosk
│   └── pedestal-app/            # Angular 18 (pantalla pedestal)
├── docs/
│   └── API.md                   # Contrato del API del Pi (para aditum-jh)
├── scripts/
│   ├── bootstrap.sh             # Instalador one-liner (virgen o retroactivo)
│   ├── configure.py             # Wizard de configuración (con hints de la instalación vieja)
│   ├── self-update.sh           # Auto-update pull-based (lock + stamps + health)
│   ├── systemd/                 # Timer del auto-update
│   ├── nginx/                   # Template del site (puerto según variante)
│   └── validate_configs.py      # Valida configs contra el schema (CI)
└── .github/workflows/ci.yml     # py_compile + node --check + validación schema
```

La config real de cada Pi vive en el backend y se cachea en
`config-runtime.json` (no versionado, junto con `device-id.txt`,
`device-token.txt`, `admin-credentials.json` y `admin-session-secret`).

## Configuración

Un único documento JSON por dispositivo (ver `config.schema.json` y los
ejemplos en `examples/`). **Se administra desde el admin de Aditum**: el
backend la guarda y la empuja al Pi con `PUT /config` (contrato completo en
[`docs/API.md`](docs/API.md)). El Pi la valida contra el schema, la escribe
atómicamente en `config-runtime.json` y se reinicia para aplicarla (PM2 lo
relanza). Sin red se opera con la última cache; sin cache, con
`config-default.json` (modo seguro: solo portones). Existe además un poller
pull de respaldo (`polling`): el editor en línea lo deja siempre activo a
40 s; `config-default.json` lo trae apagado.

Identidad del dispositivo (provisionar una vez por Pi, **no se versionan**):

```bash
echo 'GATE-CR-0034' > device-id.txt
echo '<token-del-backend>' > device-token.txt && chmod 600 device-token.txt
```

El token también puede provisionarse remotamente: un Pi sin token acepta
`PUT /token` sin credencial (TOFU — hacerlo apenas se registra el Entry
Point). Mientras no haya token, **el resto del API exige la sesión del
editor en línea** y `GET /status` lo reporta con `provisioned: false`.
También se puede provisionar desde la sección Seguridad del editor.

**Editor en línea**: en el Pi, `http://localhost:8080/admin` (o vía el Entry
Point remoto). Pide login de administrador local (semilla `admin`/`admin0606`,
cambiarla en Seguridad; hash en `admin-credentials.json`). Secciones:

- **Salud**: dashboard con prosa de la config aplicada, servicios PM2,
  lectores configurados vs USB detectados (equivale a `evtest`), CPU/RAM y
  botón de reinicio del servicio con confirmación (`GET /health`).
- **Equipo**: todo se configura eligiendo el tipo (portones / portones +
  lectores / pedestal). El editor fija las políticas simples: GPIO siempre
  modo BOARD con pulso de 1 s, polling siempre 40 s, pantalla implícita en
  el tipo pedestal. Con lectores de cámara, cada una puede mostrar su
  cuadro en tiempo real en la pantalla local (`showCameraFeed`).
- **Identidad**: nombre del lugar y `deviceId`. Cambiar el `deviceId` desde
  una sesión admin re-identifica el equipo (reescribe `device-id.txt`); un
  push del backend con otro `deviceId` se sigue rechazando (409).
- **Respaldo**: exportar la config como JSON e importar la de otro equipo
  (se aplica de inmediato, conservando identidad y revisión locales).
- **Seguridad**: provisionar token (si falta) y cambiar credenciales.

También se puede empujar por curl (`PUT /config`, ver docs/API.md) o copiar
un ejemplo a `config-runtime.json`.

Las credenciales de los terminales Hikvision **no** van en la config: llegan
en cada `POST /update-card` desde el backend (tabla `gate`).

## Seguridad del API

Todos los endpoints del Pi exigen el token del dispositivo
(`Authorization: Bearer` o `X-Device-Token`) **o una sesión del editor en
línea** (login de persona, superusuario local). Público solo: `GET /`
(health mínimo), el HTML de `/admin` y su flujo de login. Detalles, flujo de rotación en dos fases y modelo de
amenaza: [`docs/API.md`](docs/API.md). Regla operativa clave: el Entry Point
registrado en Aditum debe ser siempre la URL del túnel remoteiot, nunca un
port-forward del router.

## Instalación (un solo comando)

En cualquier Raspberry Pi (virgen **o con una instalación vieja** de
cualquier branch histórico), como en una terminal con `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/Sergiocr16/aditum-gate/production/scripts/bootstrap.sh | sudo bash
```

El instalador (`scripts/bootstrap.sh`) hace todo y es **idempotente**
(re-correrlo es el primer paso de soporte ante cualquier problema):

1. **Respalda** la instalación vieja en `~/aditum-backup-<fecha>/`: crontabs,
   sites de nginx, sudoers, lista de PM2 y el diff del working tree (las
   configs editadas a mano en los `.py` viejos).
2. **Limpia lo legacy**: PM2 del usuario pi (y su arranque systemd), crons de
   reinicio cada 10 min, sudoers de nginx, sites viejos. Nunca toca
   remoteiot/VNC/red.
3. **Instala lo nuevo**: Node 20 system-wide (NodeSource), PM2 como root con
   `ecosystem.config.js`, venv Python (`.venv/`, sobrevive a Bookworm),
   dependencias por variante (OpenCV de apt, NeoPixel) y nginx :80 → pantalla
   (:3000) o API (:8080) según la config.
4. **Configura**: lanza el wizard (`scripts/configure.py`), que sugiere como
   defaults los valores detectados en la instalación vieja (doorId,
   deviceName, etc.) y valida el resultado contra el schema.
5. **Habilita el auto-update**: systemd timer cada 15 min.

Requisitos previos en una Pi virgen: Raspbian, red, y el acceso remoto de
siempre (remoteiot con `partners@aditumcr.com`; VNC con X11 vía
`raspi-config`; `xscreensaver` deshabilitado si hay pantalla).

### Modo no interactivo (flotas)

```bash
# Con un config preparado (p.ej. exportado de otro Pi o del backend):
ADITUM_CONFIG_URL=https://.../GATE-CR-0034.json \
  curl -fsSL .../bootstrap.sh | sudo -E bash

# O por variables:
ADITUM_NONINTERACTIVE=1 ADITUM_DEVICE_ID=GATE-CR-0034 \
ADITUM_SCANNER_TYPE=hid ADITUM_API=caseta \
ADITUM_SCANNERS='[{"role":"entry","doorId":"118","deviceName":"Newtologic  4010E"}]' \
  curl -fsSL .../bootstrap.sh | sudo -E bash
```

Otras variables: `ADITUM_BRANCH` (pin de branch por Pi), `ADITUM_RECONFIGURE=1`
(forzar el wizard), `ADITUM_HOME`, `ADITUM_USER` (usuario de la instalación),
`ADITUM_CONFIG_FILE` (config preparada local, alternativa a
`ADITUM_CONFIG_URL`), `ADITUM_ADMIN_USER` / `ADITUM_ADMIN_PASSWORD` (siembra
de las credenciales del editor en línea), y las del wizard
(`ADITUM_PLACE_NAME`, `ADITUM_VERIFIER`, `ADITUM_HAS_SCREEN`,
`ADITUM_DOOR_TYPE`, `ADITUM_LOGO_URL`, `ADITUM_WATCHDOG`, `ADITUM_NEOPIXEL`,
`ADITUM_TOKEN`).

### Checklist de prueba en banco (histórico: rollout de la consolidación 2026-06)

1. **Pi virgen**: one-liner → wizard → `curl localhost:8080/` OK → push dummy
   a production → la Pi se actualiza sola en ≤15 min → segundo one-liner no rompe
   nada (idempotencia).
2. **Pi "vieja" simulada**: checkout de `pistolaqr` con edits + nvm/PM2 bajo
   pi + crons de 10 min + nginx→4200 → one-liner → verificar backup completo,
   hints correctos en el wizard y teardown limpio (`crontab -l`,
   `systemctl status pm2-pi`, sites nginx).
3. **Rollout**: 1 Pi piloto por variante (hid, opencv, hikvision, solo
   portones) 48 h antes del resto.

## Operación

```bash
sudo pm2 status          # aditum-device + aditum-web
sudo pm2 logs            # logs de ambos
curl localhost:8080/     # health minimo publico: {"status": "ok"}
curl -H "Authorization: Bearer $TOKEN" localhost:8080/health   # salud completa
curl localhost:8080/openGate/1
```

Despliegue: push a `production` → cada Pi se actualiza sola en ≤15 min
(`scripts/self-update.sh` vía systemd timer: lock anti-solape, reinstala
dependencias solo si cambiaron — stamps sha256 —, health check al final).
Una Pi puede fijarse a otro branch con `ADITUM_BRANCH=...` en
`/etc/default/aditum-gate`.

**El build de Angular está commiteado** (`web/pedestal-app/dist/`): las Pis
nunca compilan. Todo cambio en `web/pedestal-app/src` debe acompañarse de
`npm ci && npm run build` (Node 20) y commitear `dist/` en el mismo commit —
el CI lo verifica.

## Configuración de lectores QR pistola

Para cada lector escanear los códigos del manual en este orden:

Modo lectura continua
![IMG_4926](https://github.com/user-attachments/assets/61b9e8fd-4ada-4b6d-94ec-8b3d92aeeed6)

Habilitar sufijos personalizados (el sufijo `@` dispara la lectura)
![IMG_4927](https://github.com/user-attachments/assets/846fb310-255d-46f1-913e-e44ef3c76cba)

Establecer sufijo personalizado
![IMG_4928](https://github.com/user-attachments/assets/e39e7731-7b37-42bd-b7aa-887dd9d907b7)

Código hex para el sufijo `@`: `99 40` (dos veces 9, luego 40)
![IMG_4929](https://github.com/user-attachments/assets/ba37595a-54c0-4031-a5c4-8e32dabd8385)
![IMG_4930](https://github.com/user-attachments/assets/6ae4057e-897f-42cb-89c7-17ede227af7d)
![IMG_4931](https://github.com/user-attachments/assets/a7b5ea92-b9bd-4fd1-8ca4-71c6dd5a45a6)

Esperar 2 segundos entre lecturas
![IMG_4932](https://github.com/user-attachments/assets/261ec1de-e5f8-4ad1-8a21-a9657055d3a9)

[MJ-8200-User-Manual.pdf](https://github.com/user-attachments/files/26721035/MJ-8200-User-Manual.pdf)

El `deviceName` del lector (para `scanners[].deviceName` en la config) se ve
en `cat /proc/bus/input/devices`.

Con **dos pistolas del mismo modelo**, la asignación entry/exit es
determinista: los lectores se ordenan por su puerto físico USB (`Phys=` en
`/proc/bus/input/devices`) y el primer scanner de la config toma el puerto
más bajo. Para fijar explícitamente qué pistola es cada lector, usar
`scanners[].devicePhys` (parte del `Phys=`, p.ej. `usb-0000:01:00.0-1.2`).

`scanners[].strictMarkerMatch` (opcional, default `false`) restaura la
semántica del `scannerExit.py` original: si el marcador `EXIT` del QR no
coincide con el rol del lector, se deniega localmente sin consultar al
backend.

## Terminales Hikvision

La guía completa de configuración del terminal (IP estática, HTTP Listening,
token, checklist) está en la nota del vault interno: *Integración Hikvision
DS-K1T323*. Resumen del rol del Pi:

- `POST :8080/update-card` — Aditum registra el token QR rolling como tarjeta
  en los terminales (cada ~22 s).
- `POST :8080/cleanup-cards` — borra los visitantes registrados.
- Limpieza automática nocturna a las `hikvision.nightlyCleanupHour` (2 AM).
  El registro de tarjetas persiste en `hikvision-cards.json`, así que la
  limpieza sobrevive reinicios.

## Contrato del backend (config remota)

```
GET /api/gate-devices/{deviceId}/config
Authorization: Bearer <deviceToken>
If-None-Match: "<configRevision>"
→ 200 (JSON del schema) + ETag | 304 | 401 | 404
```

El backend guarda el documento completo (`config_json`), incrementa
`configRevision` en cada cambio y actualiza `last_seen_at` en cada GET
(monitoreo de Pis vivas).

Modelo sugerido (tabla `gate_device` en aditum-jh): `device_id` (unique),
`device_token_hash` (SHA-256 del token), `company_id` (FK opcional),
`config_json` (clob), `config_revision` (int), `last_seen_at` (timestamp).

## Migración desde los branches viejos (histórico: consolidación 2026-06)

Antes cada variante vivía en un branch distinto. Todo se consolidó aquí; el
branch viejo de cada Pi se reproduce con una config:

| Branch viejo | Config equivalente |
|---|---|
| `hikvision-qr` | `scannerType: "hid"` o `"hikvision"`, `verifierStyle: "secure"`, `hikvision.enabled` según el caso |
| `pistolaqr` | `scannerType: "hid"`, `baseUrl: caseta`, 2 scanners, `hasScreen: true` |
| `qr-readers` | Igual que pistolaqr pero `baseUrl: app` |
| `qr-small` | `scannerType: "none"`, `gpio.watchdog.enabled: true`, `hasScreen: false` |
| `pedestal-app` | `scannerType: "opencv"`, `hasScreen: true`, `gpio.neopixel.enabled: true` |
| `main` (viejo) | `scannerType: "opencv"`, `verifierStyle: "legacy"` |

Correspondencia de archivos eliminados → nuevos:

| Antes | Ahora |
|---|---|
| `index.js` (root, exec de scripts) | `ecosystem.config.js` (PM2) + `device/main.py` |
| `serverGPIO.py` | `device/aditum_gate/{api,gpio_relays,hikvision}.py` |
| `scannerQr.py` / `scannerQrExit.py` | `device/aditum_gate/scanners/hid.py` (2 instancias) |
| `scanner.py` / `scannerExit.py` | `device/aditum_gate/scanners/opencv.py` (2 instancias) |
| `led.py` | `device/aditum_gate/leds.py` |
| Watchdog en `index.js` de qr-small | `device/aditum_gate/watchdog.py` |
| `aditum-qr-web/` | `web/` (sirve el build en :3000, sin ng serve en :4200) |
| Cron de reinicio cada 10 min | PM2 autorestart + watchdog + `POST :8080/restart` |
| Branch `auto` (config-manager.js/py) | `device/aditum_gate/config_agent.py` + `web/server.js` |

Para migrar una Pi existente basta el instalador one-liner (sección
"Instalación"): respalda la instalación vieja, sugiere los valores detectados
(doorIds, deviceName, etc.) en el wizard, limpia los crons/PM2 legacy e
instala todo. Después: provisionar el token y registrar el Entry Point en
Aditum. Los branches viejos quedan como tags `legacy/*`.
