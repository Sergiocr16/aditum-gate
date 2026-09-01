---
name: migrar-config-vieja
description: Convierte una instalacion vieja de aditum-gate (config hardcodeada en .py/.ts/.js) al config-runtime.json nuevo, reusando configure.py --hints. Usar cuando el usuario diga "migra este equipo viejo", "pasa la config vieja al json", "genera la config desde el codigo viejo", "convierte la instalacion vieja" o /migrar-config-vieja.
---

# Migrar una instalacion vieja de aditum-gate al JSON nuevo

Sos el tecnico de migracion. Objetivo: tomar una instalacion **vieja** (config
hardcodeada en `scanner*.py`, `serverGPIO.py`, `led.py`, `index.js`,
`app.component.ts`) y producir el `config-runtime.json` de la version nueva con
los valores reales del equipo.

**La maquinaria de extraccion ya existe: no escribas un parser.** Reusa
`scripts/configure.py --hints DIR`, que cosecha los valores viejos y pre-rellena
el wizard. Tu trabajo es orquestar ese flujo y cubrir a mano los pocos valores
que el cosechador aun no extrae.

## Los 3 puntos que definen el equipo (referencia rapida)

Casi todo se decide mirando estos tres archivos. Ejemplo real de valores:

**1. `$OLD/index.js` — que usa el equipo:**

```js
const qrCodeReader  = false;   // true = lectores/pistolas HID · false = camaras
const hasScreen     = false;
const hasTwoCameras = false;   // true = dos lectores/camaras · false = uno
```

- `qrCodeReader = true`  -> corre `scannerQr.py` (+`scannerQrExit.py` si dos) -> **`scannerType: "hid"`**
- `qrCodeReader = false` -> corre `scanner.py` (+`scannerExit.py` si dos) = **camaras** -> **`scannerType: "opencv"`**
- `hasTwoCameras`/dos scripts -> **2 entradas** en `scanners[]` (entry+exit); si no, 1.

**2. Cabecera del scanner que corre — las variables clave:**

```py
# scannerQr.py (HID)            # scanner.py (camara)
doorType = "entry"             doorType = "exit"        # -> scanners[].role
doorId   = '0'                 doorId   = '0'           # -> scanners[].doorId (PLACEHOLDER)
placeName = 'Name'             placeName = 'Name'       # -> placeName (PLACEHOLDER)
hasScreen = True               showCameraFeed = True    # -> scanners[].showCameraFeed
                               hasScreen = False        # -> screen.hasScreen
```

**3. `$OLD/aditum-qr-web/pedestal-app/src/app/app.component.ts` — la pantalla:**

```ts
doorType: string = 'EXIT';                              // -> screen.doorType (ENTRY|EXIT)
clientLogoUrl: string = 'https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg';
                                                        // -> screen.clientLogoUrl (el generico es PLACEHOLDER)
```

Recorda: `doorId='0'`, `placeName='Name'` y el logo generico de Cloudinary son
**plantillas sin personalizar**; los valores reales del equipo hay que
confirmarlos con el operador / Aditum.

## Reglas (no negociables)

- NUNCA edites `config-runtime.json`, `device-id.txt` ni `device-token.txt` a
  mano. El unico escritor sancionado es `configure.py` (valida y confirma antes
  de escribir). El backend/`/admin` es la via productiva.
- NO inventes `deviceId`, token ni `doorId`: salen de Aditum o del operador.
- Los valores del REPO viejo de muestra (`/home/pi/aditum-gate`) son
  **placeholders** (`doorId='0'`, `placeName='Name'`, logo generico). Si estas
  cosechando de ahi, avisale al operador que son plantillas a reemplazar; los
  valores reales viven en el disco de la Pi vieja que se reemplaza.
- Credenciales Hikvision (user/password) NO van en la config: llegan por
  `POST /update-card` desde el backend.

## Flujo

### 1. Localizar la instalacion vieja

Buscar en este orden y usar el primero que exista:

```bash
ls -d /home/*/aditum-backup-*/old-app 2>/dev/null   # lo deja bootstrap.sh
ls -d /home/pi/aditum-gate 2>/dev/null              # repo viejo (o de muestra)
```

Si no hay ninguno, pedile al operador el path. Confirmar que contiene los
scripts viejos:

```bash
ls "$OLD/scanner"*.py "$OLD/serverGPIO.py" "$OLD/led.py" 2>/dev/null
ls "$OLD/aditum-qr-web/pedestal-app/src/app/app.component.ts" 2>/dev/null
```

`$OLD` = ese directorio de aca en adelante.

### 2. Detectar la variante

Leer para deducir `scannerType` y cuantos lectores:

- `$OLD/index.js`: `hasScreen`, `hasTwoCameras`, `qrCodeReader`.
- `$OLD/serverGPIO.py`: `isScreen` (pedestal vs relays), la lista `gates`, y si
  hay rutas ISAPI `http://{ip}/ISAPI/AccessControl/...` (Hikvision).
- Que `scanner*.py` existen:
  - `scannerQr.py` / `scannerQrExit.py` presentes -> **`hid`** (lector USB evdev).
  - `scanner.py` / `scannerExit.py` presentes -> **`opencv`** (camara).
  - Solo ISAPI en `serverGPIO.py`, sin scanner -> **`hikvision`**.
  - Solo relays, sin scanner ni pantalla -> **`none`**.

### 3. Cosechar los valores viejos

El cosechador vive en `configure.py`. Primero, mira que cosecha de este equipo
(no escribe nada):

```bash
cd /home/pi/aditum-gate   # el repo NUEVO (este)
PY=$([ -x .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3)
$PY -c 'import importlib.util,json; s=importlib.util.spec_from_file_location("c","scripts/configure.py");
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.harvest_hints("'"$OLD"'"),
indent=2, ensure_ascii=False))'
```

`harvest_hints()` extrae `doorId`, `doorType`, `placeName`, `hasScreen`,
`showCameraFeed` y `DEVICE_NAME`, y mapea cada archivo a (tipo, rol):
`scannerQr.py`->(hid,entry), `scannerQrExit.py`->(hid,exit),
`scanner.py`->(opencv,exit), `scannerExit.py`->(opencv,exit).

Luego, segun quien corre la migracion, elegi UNA via:

- **Operador en la Pi (terminal interactiva):** el wizard pre-rellenado.
  Requiere el venv (bootstrap ya lo creo; si no existe, corre el bootstrap
  primero). Lo corre el operador, no el agente:

  ```bash
  .venv/bin/python3 scripts/configure.py --hints "$OLD"
  ```

- **Agente / sin TTY:** el wizard es interactivo (`input()`), asi que el
  agente NO puede responderlo. En su lugar: junta hints + huecos (paso 4) +
  las respuestas del operador por chat, arma el JSON candidato, guardalo en el
  scratchpad y validalo/escribilo con `--from-file` (valida contra el schema y
  no pide prompts):

  ```bash
  .venv/bin/python3 scripts/configure.py --from-file /ruta/candidate.json --device-id GATE-CR-XXXX
  ```

  Sin venv en un banco de pruebas, valida el candidato con el `python3` de
  sistema (necesita `jsonschema`) reusando `configure.validate()` — sin
  persistir nada.

### 4. Cubrir los huecos del cosechador

`harvest_hints()` todavia NO extrae estos valores; leelos a mano de `$OLD` y
metelos en el prompt correspondiente del wizard:

| Campo del JSON | De donde leerlo en `$OLD` |
|---|---|
| `api.baseUrl` | Host de las URLs `aditum-gate-verifier-*`: `app.` vs `caseta.aditumcr.com` (grep en los `scanner*.py`). |
| `api.verifierStyle` | **Ya no se usa** (obsoleto, se ignora): los QR `ADTG` y `ADITUMGATE=` se aceptan siempre y el prefijo de cada codigo decide el endpoint. No ponerlo. |
| `scanners[].cameraIndex` | `cv2.VideoCapture(N)` en `scanner.py`/`scannerExit.py`. |
| `scanners[].strictMarkerMatch` | `true` solo donde el viejo casaba `EXIT`<->rol (`scannerExit.py`); `false` en el resto. |
| `screen.doorType` | `doorType` en `app.component.ts` (`ENTRY`/`EXIT`). |
| `screen.clientLogoUrl` | `clientLogoUrl` en `app.component.ts`. |
| `gpio.neopixel.pin` / `.count` | `led.py` (`board.D18`, `55`). `enabled=true` solo si el equipo tiene tira. |
| `deviceId` + token | NO estan en el viejo: los provisiona el operador/backend. |

### 5. Tabla de mapeo de referencia (viejo -> JSON nuevo)

| Viejo (hardcodeado) | Campo nuevo | Nota |
|---|---|---|
| `placeName` en `scanner*.py` | `placeName` | Placeholder en el repo de muestra. |
| Host de URLs verifier | `api.baseUrl` | El viejo mezclaba `app.`/`caseta.`; elegir uno. |
| Prefijo `ADITUMGATE=`, endpoints sin `-secure` | (nada) | Ambos prefijos se aceptan siempre; el `ADITUMGATE=` va solo al endpoint legacy. |
| Script que corria (camara/HID/ISAPI/relays) | `scannerType` | opencv/hid/hikvision/none. |
| `doorType = "entry"/"exit"` | `scanners[].role` | |
| `doorId` | `scanners[].doorId` | `'0'` es placeholder; el real lo da Aditum. |
| `DEVICE_NAME = "Newtologic  4010E"` | `scanners[].deviceName` | Copiar literal: **doble espacio**. |
| `/dev/input/event0`/`event5` fijos | `scanners[].devicePhys` | El nuevo distingue por puerto USB, no por numero de event. |
| `cv2.VideoCapture(N)` | `scanners[].cameraIndex` | |
| `showCameraFeed = True` | `scanners[].showCameraFeed` | |
| `hasScreen`/`isScreen` | `screen.hasScreen` | |
| `doorType`/`clientLogoUrl` (app.component.ts) | `screen.doorType`/`screen.clientLogoUrl` | |
| gates id->pin en `serverGPIO.py` | `gpio.gates[]` | Ya identicos en `config-default.json`; no tocar. |
| `time.sleep(1)` del pulso | `gpio.pulseSeconds: 1` | Fijo. |
| `board.D18`, `55` (`led.py`) | `gpio.neopixel` | |
| `target_hour = 2` (Hikvision) | `hikvision.nightlyCleanupHour: 2` | |

### 6. Que ya NO se configura (la version nueva lo fija)

No busques equivalente para: pines/modo BOARD/pulso 1s (ya en el default),
prefijos QR, puertos 3000/8080, kiosk chromium, nginx, el puerto viejo 7777,
los mapas de teclado de los lectores. La version nueva los maneja sola.

### 7. Validar y revisar

`configure.py` ya valida contra `config.schema.json` en loop y muestra el JSON
antes de guardar. Antes de confirmar:

- Verifica que la variante y los `doorId` sean los reales (no placeholders).
- Si preferis NO escribir identidad todavia, cancela el guardado y cargá el
  JSON revisado por `/admin`, o dejá que el backend lo empuje con `PUT /config`.

Revalidacion manual de un JSON candidato:

```bash
.venv/bin/python3 scripts/validate_configs.py
```

### 8. Reporte final

Mostrar al operador SOLO los valores leidos de los 3 puntos (seccion
"referencia rapida"), con su valor exacto y a que campo del JSON van. Nada mas.
Formato:

```
1) index.js
   qrCodeReader = <valor>   -> scannerType: <hid|opencv>
   hasScreen    = <valor>
   hasTwoCameras= <valor>   -> <1|2> lector(es)

2) <scannerQr.py|scanner.py>
   doorType       = <valor> -> scanners[].role
   doorId         = <valor> -> scanners[].doorId   <(PLACEHOLDER) si es '0'>
   placeName      = <valor> -> placeName            <(PLACEHOLDER) si es 'Name'>
   showCameraFeed = <valor> -> scanners[].showCameraFeed
   hasScreen      = <valor> -> screen.hasScreen

3) app.component.ts
   doorType      = <valor>  -> screen.doorType
   clientLogoUrl = <valor>  -> screen.clientLogoUrl <(PLACEHOLDER) si es el generico>
```
