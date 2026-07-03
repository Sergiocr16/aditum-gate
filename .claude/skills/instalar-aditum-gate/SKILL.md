---
name: instalar-aditum-gate
description: Instala o repara aditum-gate en esta Raspberry Pi y la deja funcionando y verificada (deps pinneadas, servicios, config, diagnostico). Usar cuando el usuario diga "instala este pi", "provisiona esta raspberry", "deja este equipo funcionando", "repara la instalacion", "chequea que este todo instalado" o /instalar-aditum-gate.
---

# Instalar / reparar una Raspberry Pi de aditum-gate

Sos el tecnico de instalacion. Objetivo: dejar ESTE equipo instalado con las
versiones pinneadas, configurado, con servicios corriendo y verificado de
punta a punta. Todo el proceso es idempotente: correr sobre un equipo ya
instalado solo repara lo que falte.

## Reglas de seguridad (no negociables)

- NUNCA toques `device-id.txt`, `device-token.txt`, `admin-credentials.json`
  ni `config-runtime.json` a mano; se administran por el editor /admin o el
  backend. Si un paso los requiere, decile al operador que lo haga en /admin.
- NUNCA abras portones (`/openGate`) sin que el operador lo pida explicito.
- Si vas a probar `create_app()` con codigo Python, sandboxea SIEMPRE los
  paths de credenciales (ver CLAUDE.md).
- No detengas `aditum-update.timer` salvo que estes desarrollando codigo.

## Flujo

### 1. Diagnostico inicial

```bash
sudo bash scripts/doctor.sh
```

- Si el repo no existe todavia (Pi virgen), primero el bootstrap (paso 2).
- Anota cada FAIL/WARN: son tu lista de trabajo. 0 FAIL → salta al paso 4.

### 2. Bootstrap (instala/repara todo)

```bash
curl -fsSL https://raw.githubusercontent.com/Sergiocr16/aditum-gate/production/scripts/bootstrap.sh | sudo bash
```

(o `sudo bash scripts/bootstrap.sh` si el repo ya esta en `/home/pi/aditum-gate`).

Es idempotente y retroactivo: desmonta instalaciones viejas, deja Node 20,
venv con deps **pinneadas** (`device/requirements*.txt`), PM2 root, nginx,
timer de auto-update y extras por variante (opencv/neopixel) segun la config.

### 3. Re-diagnostico y reparacion dirigida

```bash
sudo bash scripts/doctor.sh
```

Repetir hasta 0 FAIL. Guia rapida por sintoma:

| FAIL | Arreglo |
|---|---|
| version pip distinta al pin | `.venv/bin/pip install -r device/requirements.txt` (y el de la variante) |
| venv sin system-site-packages | `sed -i 's/include-system-site-packages = false/...= true/' .venv/pyvenv.cfg` + `pm2 restart aditum-device` |
| cv2/pyzbar no importable | `sudo apt-get install python3-opencv libzbar0 uhubctl && .venv/bin/pip install -r device/requirements-opencv.txt` |
| PM2 no online | `sudo pm2 start ecosystem.config.js && sudo pm2 save`; ver logs `sudo pm2 logs aditum-device --lines 50` |
| timer inactivo | `sudo systemctl enable --now aditum-update.timer` |
| config no valida contra schema | revisar en /admin (el formulario normaliza al guardar) |
| sin camaras USB / lector HID no detectado | problema fisico: cable/puerto; verificar con `GET /health` |

### 4. Configuracion

- Sin `config-runtime.json`: abrir `http://localhost:8080/admin` (o el wizard
  `sudo .venv/bin/python3 scripts/configure.py`) y elegir el tipo de equipo
  (portones / portones + lectores / pedestal). El editor fija las politicas.
- La config productiva la empuja el backend de Aditum (`PUT /config`); lo
  local es para el arranque inicial o el banco.

### 5. Verificacion funcional (segun variante)

```bash
sudo bash scripts/doctor.sh        # debe dar 0 FAIL
curl -s localhost:8080/ | grep ok  # API viva
```

- **Lectores HID**: `GET /health` (con sesion admin) debe listar el lector en
  `inputDevices` y `readers[].connected=true`. Pedirle al operador escanear
  un QR de prueba y mirar `sudo pm2 logs aditum-device`.
- **Camaras**: `readers[].connected=true` en /health; sin crash-loop en
  `sudo pm2 logs aditum-device` (si `showCameraFeed` esta activo, la ventana
  aparece en la pantalla local).
- **Pantalla (pedestal)**: chromium en kiosko apuntando a :3000; `aditum-web`
  online en PM2.
- **Portones**: NO abrir por tu cuenta. Decirle al operador que pruebe desde
  Aditum o /admin.
- **Hikvision**: `POST /update-card` lo prueba el backend; verificar solo que
  `hikvision.enabled` este acorde al tipo.

### 6. Token e identidad

- `GET /status` → `provisioned`. Sin token el API queda **abierto** (modo
  compatibilidad); el backend lo provisiona con `PUT /token` al registrar el
  Entry Point. Recordarselo al operador si queda en `false`.
- La identidad (`deviceId`) se define en /admin → Identidad.

### 7. Reporte final

Entregar al operador: salida del doctor (0 FAIL), variante configurada,
identidad, estado del token, y que quedo pendiente (p.ej. "provisionar token
desde Aditum", "conectar la camara del carril 2").
