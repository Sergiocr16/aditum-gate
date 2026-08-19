# API del dispositivo Aditum Gate

Contrato del API HTTP que expone cada Raspberry Pi (Flask, puerto **8080**)
para que el backend de Aditum (aditum-jh) lo administre. La URL base de cada
dispositivo es su **Entry Point** registrado en Aditum (la URL del túnel
remoteiot — ver [Modelo de amenaza](#modelo-de-amenaza)).

El nginx `:80` de cada Pi enruta las rutas de este API a Flask `:8080` en
**todas las variantes** — en los pedestales el resto de la URL (`/`) sirve
la pantalla. O sea: una sola URL de túnel por equipo alcanza para el Entry
Point, el editor `/admin` y ver la pantalla; no hace falta un mapeo aparte
al `:8080`. (Excepción: `GET /` en pedestales responde la pantalla, no el
health del API — para health por túnel usar `GET /status` o `GET /health`.)

> **⚠️ Breaking change / checklist de migración para aditum-jh**
>
> Todos los endpoints (salvo `GET /`) ahora exigen el **token del
> dispositivo**. El backend debe agregar el header de autenticación a TODAS
> sus llamadas existentes al Pi, no solo a las nuevas:
>
> - [ ] `GET /openGate/<id>` y `GET /closeGate/<id>`
> - [ ] `GET /gateStatus` y `GET /gateStatus/<id>`
> - [ ] `POST /update-card` y `POST /cleanup-cards`
> - [ ] `GET /code-accepted/<name>`, `/code-denied/<name>`, `/wait-for-response/<name>`
> - [ ] `POST /restart`
> - [ ] Generar y provisionar un token por dispositivo (ver
>       [Provisión](#provisión-y-rotación-del-token))
>
> Un Pi **sin token provisionado** deja el API **abierto** (compatibilidad:
> los portones de la flota siguen funcionando aunque el backend aún no mande
> el header). Se delata en `GET /status` con `"provisioned": false` y loguea
> ERROR de forma continua. **Apenas se provisiona el token** (`PUT /token`
> TOFU o `device-token.txt`) el enforcement pasa a ser estricto: 401 sin
> credencial. La meta es que ningún Pi quede sin provisionar.

## Autenticación

Hay **dos credenciales válidas**; cualquiera de las dos pasa el
`before_request` global:

1. **Token del dispositivo** (para el backend, máquina a máquina). Opaco,
   32 bytes hex recomendado, generado por Aditum. Se envía en cualquiera de
   los dos headers:

   ```
   Authorization: Bearer <token>
   X-Device-Token: <token>
   ```

2. **Sesión de administrador** (para una persona en el editor en línea
   `/admin`): login usuario/contraseña → cookie firmada HttpOnly (8 h). Una
   sesión admin es superusuario del API local. Ver
   [Login del editor](#login-del-editor-en-línea).

Reglas:

- Público (sin credencial) solo: `GET /`, el HTML de `GET /admin` y su flujo
  de login (`/admin/login`, `/admin/logout`, `/admin/session`).
- Respuesta sin/mal credencial: `401 {"error": "unauthorized"}` (también para
  rutas inexistentes — no hay enumeración de endpoints).
- El backend guarda **solo el SHA-256** del token, nunca el token en claro.
- El token **jamás debe llegar al browser** del admin: todas las llamadas al
  Pi se hacen desde el servidor de aditum-jh (proxy), nunca desde Angular.

## Endpoints

Índice rápido (P = público, T/S = exige token del dispositivo o sesión admin):

| Endpoint | Método | Auth | Para qué |
|---|---|---|---|
| `/` | GET | P | Health mínimo |
| `/admin` | GET | P | HTML del editor en línea |
| `/admin/session`, `/admin/login`, `/admin/logout` | GET/POST | P | Flujo de login del editor |
| `/admin/password` | POST | S | Cambiar credenciales del editor |
| `/status` | GET | T/S | Identidad, revisión, provisioned |
| `/health` | GET | T/S | Salud: servicios, USB, lectores, sistema |
| `/config` | GET / PUT | T/S | Leer / aplicar la configuración |
| `/backup` | GET | T/S | Config en formato exportable (respaldo) |
| `/token` | PUT | especial | Provisión TOFU / rotación |
| `/token` | DELETE | T/S | Desprovisionar el equipo |
| `/gateStatus[/<id>]` | GET | T/S | Estado de portones |
| `/openGate/<id>`, `/closeGate/<id>` | GET | T/S | Pulso de apertura/cierre |
| `/update-card`, `/cleanup-cards` | POST | T/S | Tarjetas Hikvision |
| `/code-*`, `/wait-for-response/<name>` | GET | T/S | Estados de pantalla (compat) |
| `/restart` | POST | T/S | Reiniciar el proceso |

### Salud y estado

#### `GET /` — público
Health mínimo, sin información del dispositivo.
```json
{"status": "ok"}
```

#### `GET /admin` — público (solo HTML)
Editor local de configuración: en el Pi se accede como
`http://localhost:8080/admin`. La página es estática y no revela nada; los
datos que muestra y guarda salen de `GET /config`, `PUT /config` y
`GET /health`, que exigen sesión de administrador (login de la propia página,
cookie firmada HttpOnly) o el token del dispositivo. Útil para técnicos en
sitio sin pasar por el admin de Aditum.

### Login del editor en línea

Autentica a una **persona** (no al backend). Credenciales en
`admin-credentials.json` del Pi (hash PBKDF2; semilla inicial
`admin`/`admin0606`, cambiable). La cookie de sesión dura 8 horas.

| Endpoint | Método | Notas |
|---|---|---|
| `/admin/session` | GET | Público. `{"authenticated": true\|false, "username": ...}` |
| `/admin/login` | POST | Público. Body `{"username", "password"}` → `200 {"ok": true}` con cookie, o `401 {"error": "credenciales invalidas"}` (con freno anti fuerza bruta) |
| `/admin/logout` | POST | Público. Cierra la sesión → `{"ok": true}` |
| `/admin/password` | POST | **Exige sesión**. Body `{"username", "currentPassword", "newPassword"}`. `401` sin sesión, `403` contraseña actual incorrecta, `400` nueva inválida (6–256 caracteres) |

Estos endpoints son para la página `/admin`; el backend de aditum-jh no los
usa (usa el token).

#### `GET /status` — protegido
```json
{
  "deviceId": "GATE-CR-0034",
  "placeName": "Condominio X",
  "scannerType": "hid",
  "configRevision": 42,
  "schemaVersion": 1,
  "provisioned": true,
  "configSource": "config-runtime.json",
  "hasScreen": true,
  "gates": [1, 2],
  "hikvisionEnabled": false,
  "pollingEnabled": false
}
```
- `provisioned: false` → mostrar badge **"SIN TOKEN"** en el admin.
- `configSource: "config-default.json"` → el Pi está operando en modo
  fallback (nunca recibió config); mostrarlo como alerta.
- `schemaVersion` es la versión de schema que **soporta el código** del Pi;
  el editor del admin debe usar el schema correspondiente.

#### `GET /health` — protegido
Reporte de salud para diagnóstico en sitio (lo consume la sección "Salud" de
`/admin`; también puede consultarlo el backend con el token). Incluye los
dispositivos de entrada USB conectados (equivalente a `evtest`), el cruce con
los lectores configurados, el estado de los procesos PM2 y del server web
`:3000`, y métricas básicas del sistema.
```json
{
  "services": [
    {"name": "aditum-device", "label": "Controlador", "online": true,
     "pid": 2305, "status": "online", "restarts": 0, "uptimeSec": 5400},
    {"name": "aditum-web", "label": "Pantalla / WebSocket", "online": true,
     "pid": 2307, "status": "online", "restarts": 0, "uptimeSec": 5400},
    {"name": "web-server-3000", "label": "Server web :3000", "online": true,
     "httpStatus": 200}
  ],
  "pm2Available": true,
  "inputDevices": [
    {"path": "/dev/input/event4", "name": "Newtologic  4010E",
     "phys": "usb-3f980000.usb-1.2/input0"}
  ],
  "cameras": [
    {"index": 0, "name": "NexiGo N60 FHD Webcam"}
  ],
  "readers": [
    {"role": "entry", "doorId": "34", "deviceName": "Newtologic  4010E",
     "devicePhys": null, "connected": true,
     "paths": ["/dev/input/event4"],
     "devices": [{"port": "usb-3f980000.usb-1.2", "path": "/dev/input/event4"}],
     "ports": ["usb-3f980000.usb-1.2"]}
  ],
  "system": {"cpuTempC": 52.1, "uptimeSec": 86400,
             "memAvailableMb": 512, "memTotalMb": 944},
  "kioskExpected": false
}
```
- `web-server-3000` es un check HTTP real contra `:3000`: PM2 puede reportar
  el proceso `online` con el puerto muerto; este campo distingue ambos casos.
- Si el equipo **no tiene pantalla**, `services` trae solo `aditum-device`:
  `aditum-web` y `web-server-3000` no aplican y se omiten
  (`kioskExpected: false`).
- `pm2Available: false` → no se pudo consultar PM2 (p.ej. banco de dev);
  los servicios traen solo lo verificable (`online` por check directo).
- `cameras` son las webcams USB de captura detectadas (`index` es el
  `cameraIndex` a usar en la config); el editor las sugiere en el
  formulario. Excluye los códecs del SoC.
- `readers[].ports` son los puertos USB físicos detectados para el nombre
  del lector (el `Phys=` sin el sufijo `/inputN`, estable entre reinicios).
  Con dos pistolas idénticas, fijar `scanners[].devicePhys` en la config
  con uno de esos valores ancla el lector a ese puerto (el editor lo ofrece
  como "Puerto USB"); sin fijarlo, la asignación es automática por orden de
  puerto según la posición del lector en `scanners[]`. Con `devicePhys`
  fijado, `connected`/`paths`/`devices` reflejan solo ese puerto.
- `readers[].devices` agrupa lo detectado por **pistola física**: una entrada
  por puerto con su nodo de teclado (el `eventX` más bajo; un aparato expone
  varios nodos `/inputN`). `paths` sigue esa agrupación — dos rutas ahí
  significan dos pistolas, no dos nodos de la misma.
- `readers[].connected: null` → el equipo no usa lector local
  (`scannerType` hikvision/none).
- Los campos de `system` pueden venir `null` si esa lectura falló.

### Configuración

El documento de configuración está definido por
[`config.schema.json`](../config.schema.json) (ese archivo ES el contrato:
el editor del admin valida client-side contra él antes de enviar). Ejemplos
por variante en [`examples/`](../examples/).

#### `GET /config` — protegido
```json
{ "config": { ...documento completo... }, "source": "config-runtime.json" }
```

#### `GET /backup` — protegido

Devuelve el **documento de configuración completo, EXACTAMENTE en el formato
de exportación/importación** del editor (sección Respaldo de `/admin`) — sin
wrapper. Pensado para que Aditum lo guarde tal cual en su BD (columna
sugerida en `gate_access`: `config_backup` clob + `backed_up_at`) y lo
ofrezca para descarga: si la SD de una Raspberry muere, ese archivo se
importa directo en el equipo nuevo.

```json
{ "schemaVersion": 1, "configRevision": 42, "deviceId": "GATE-CR-0034",
  "placeName": "Condominio X", "api": { ... }, "scannerType": "hid",
  "scanners": [ ... ], "screen": { ... }, "gpio": { ... },
  "hikvision": { ... }, "polling": { ... } }
```

- A diferencia de `GET /config`, trae **siempre** el `deviceId` efectivo del
  equipo (`device-id.txt` manda: una config pushada genérica puede venir sin
  él), y la respuesta es el documento pelado, restaurable sin transformación.
- **Cuándo respaldar**: tras cada `PUT /config` exitoso y/o un pull diario.
- **Cómo restaurar en un equipo nuevo**: instalarlo (bootstrap + provisión de
  token) y luego (a) descargar el JSON de Aditum e importarlo en
  **Respaldo → Importar** del editor `/admin` (aplica y reinicia solo, y
  conserva la identidad del equipo nuevo), o (b) `PUT /config` con el JSON
  tal cual (si el `deviceId` difiere del equipo destino, quitarlo del JSON o
  re-identificar primero).
- Lo que NO incluye (a propósito): `device-token.txt` (secreto — el token del
  equipo nuevo se provisiona aparte), credenciales del editor y
  `hikvision-cards.json` (se regenera solo: las tarjetas se re-registran
  cada ~22 s).

#### `PUT /config` — protegido
Body: el documento completo. Reglas:

- `configRevision` la administra el **backend**: incrementarla en cada
  guardado. El Pi la usa para orden e idempotencia.
- `deviceId` del body debe coincidir con el del Pi o ir vacío (es
  informativo; la identidad no se cambia por config, vive en `device-id.txt`).

Respuestas:

| Caso | Status | Body |
|---|---|---|
| Aplicada con reinicio | 200 | `{"applied": true, "willRestart": true, "revision": 43}` |
| Aplicada sin reinicio (solo `polling`) | 200 | `{"applied": true, "willRestart": false, "revision": 43}` |
| Idéntica a la vigente (retry) | 200 | `{"applied": false, "willRestart": false, "revision": 42}` |
| Revisión vieja | 409 | `{"error": "stale revision", "currentRevision": 42}` |
| `deviceId` de otro Pi | 409 | `{"error": "deviceId mismatch", "expected": "GATE-CR-0034"}` |
| `deviceId` inválido (solo sesión admin) | 400 | `{"error": "invalid config", "details": ["deviceId: maximo 128 caracteres, sin espacios"]}` |
| `schemaVersion` no soportada | 409 | `{"error": "unsupported schemaVersion", "supportedSchemaVersion": 1}` |
| No valida contra el schema | 400 | `{"error": "invalid config", "details": ["...mensajes jsonschema..."]}` |
| Body que no es objeto JSON | 400 | `{"error": "body must be a JSON object"}` |

**Re-identificación local**: el 409 `deviceId mismatch` aplica a los push
autenticados con token (protege contra aplicar la config de otra Pi). Una
**sesión admin** del editor local sí puede mandar un `deviceId` distinto:
se acepta, se reescribe `device-id.txt` y el equipo queda re-identificado
(máx. 128 caracteres, sin espacios).

**Contrato post-restart**: con `willRestart: true` el Pi se reinicia ~1 s
después de responder y queda inaccesible **5–15 s**. El backend debe:
pollear `GET /status` hasta obtener 200 (no `GET /`: en pedestales esa
ruta la responde la pantalla, no el API) y confirmar que
`configRevision` es la nueva (si no lo es, la config cacheada falló al
cargar y el Pi cayó al default — alertar). Serializar los pushes: **un
`PUT /config` en vuelo por dispositivo a la vez**.

Durante esa ventana (y cualquier otro reinicio del proceso), si el Entry
Point pasa por el nginx `:80` del Pi la respuesta es **`503` con header
`Retry-After: 5`** (una página HTML de espera, no JSON): tratar 502/503/504
del Pi como transitorio y reintentar con backoff, nunca como fallo
definitivo del dispositivo.

```bash
curl -X PUT http://<entry-point>/config \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @config.json
```

### Token

#### `PUT /token` — protegido (abierto solo si el Pi no está provisionado)
Body: `{"token": "<nuevo token>"}` (16–256 caracteres, sin espacios).

- Primera provisión (TOFU): `200 {"provisioned": true}`.
- Rotación: autenticada con el token vigente → `200 {"rotated": true}`.
  Efecto inmediato.
- Token inválido: `400 {"error": "invalid token", "details": [...]}`.

#### `DELETE /token` — protegido

Desprovisiona el equipo: borra `device-token.txt` con efecto inmediato →
`200 {"deprovisioned": true}` (idempotente). El equipo vuelve al estado
TOFU: `GET /status` reporta `provisioned: false`, el API queda **abierto**
(modo compatibilidad) y `PUT /token` acepta un token nuevo sin credencial —
**re-provisionar de inmediato**, igual que en la instalación. El backend la
usa al desvincular o resetear un equipo.
Firmware viejo sin este endpoint responde `405` (con credencial válida) o
`401` (sin ella).

### Portones

| Endpoint | Método | Respuesta |
|---|---|---|
| `/openGate/<id>` | GET | `{"id": 1, "status": 0}` — pulso de apertura (1 s) |
| `/closeGate/<id>` | GET | `{"id": 1, "status": 0}` |
| `/gateStatus` | GET | `[{"id":1,"pin":7,"normallyOpen":true,"status":0}, ...]` |
| `/gateStatus/<id>` | GET | `{"value": 1}` |

Portón inexistente → `404 {"error": "..."}`.

Cada portón puede configurarse como **normalmente abierto** (default) o
**normalmente cerrado** vía `gpio.gates[].normallyOpen` en la config (el
contrato del shape sigue siendo `config.schema.json`). `openGate` pulsa el
**nivel activo** del relé: LOW si es normalmente abierto, HIGH si
`normallyOpen: false`; `closeGate` vuelve el relé a reposo en ambos casos.
`/gateStatus/<id>.value` es el nivel **físico** crudo del pin: en un portón
normalmente cerrado el reposo lee `0`.

> **Reintentos**: `GET /openGate/<id>` NO es idempotente (cada llamada
> re-pulsa el relay). Usar timeout de 5 s y **no** reintentar a ciegas.
> `PUT /config` sí es seguro de reintentar (idempotente por revisión).
> `POST /update-card` es idempotente.

### Hikvision

| Endpoint | Método | Notas |
|---|---|---|
| `/update-card` | POST | `{"cardNo", "employeeNo", "terminals": [{ip,user,password}]}` — las credenciales de los terminales viajan en el payload (vienen de la tabla `gate`), **no** en la config |
| `/cleanup-cards` | POST | Borra todos los visitantes registrados |

Si el Pi no tiene Hikvision habilitado → `400`.

### ANPR local-first (cámaras de placas)

La lista de placas autorizadas vive **dentro de la cámara ANPR**: la cámara
autoriza y abre sola, sin internet. Aditum mantiene esa lista empujando
cambios al Pi, y las lecturas viajan de vuelta por una cola local que
sobrevive cortes de conexión. Análogo a `/update-card` con terminales QR,
pero contra la lista de placas (ISAPI `licensePlateAuditData`).

| Endpoint | Método | Auth | Notas |
|---|---|---|---|
| `/update-plate` | POST | token | Alta o baja de UNA placa. Contrato de `AnprPlateSyncDispatchService` (TAR-1033) |
| `/sync-plates` | POST | token | Reemplazo COMPLETO de la lista de la cámara (TAR-1037) |
| `/anpr-event` | POST | **público** + filtro por IP | Lo postea la cámara (no sabe mandar bearer). Solo encola: no abre portones ni toca configuración |
| `/anpr-status` | GET | token | Pendientes/enviados de la cola y los últimos 5 eventos (soporte y piloto) |

Si el Pi no tiene ANPR habilitado (`anpr.enabled = false`) → `400` en los dos
primeros y `404` en los dos últimos.

**`POST /update-plate`** — cuerpo:

```json
{
  "requestId": "48211", "action": "ADD",
  "cameraId": 12, "plate": "ABC-123", "plateNormalized": "ABC123",
  "cameras": [{ "ip": "10.8.0.31", "user": "admin", "password": "..." }]
}
```

`plateNormalized` (`[A-Z0-9]+`) es la **identidad** de la placa y es lo que se
guarda en la cámara; `plate` es informativa. Las credenciales de la cámara
viajan en el payload (el Pi es *stateless*: las usa y las descarta, igual que
`terminals` en `/update-card`).

**`POST /sync-plates`** — igual, con `"plates": [{plate, plateNormalized}, …]`
en vez de una placa; deja la cámara exactamente con esa lista.

**Idempotencia** (obligatoria por contrato): `ADD` de una placa ya presente
responde `200` con `detail: "already_present_noop"`, y `DELETE` de una
inexistente `200` con `"not_found_noop"`. El backend recalcula el estado
deseado contra la base antes de despachar, así que reintentar nunca es
incorrecto.

**Códigos que devuelve el Pi** (el backend solo mira el HTTP: `2xx` = éxito,
cualquier otra cosa = fallo reintentable):

| HTTP | Cuándo | `error` |
|---|---|---|
| `200` | Aplicado o no-op idempotente | — |
| `400` | Falta `action`, `plateNormalized` o `cameras` | — |
| `401` | Bearer ausente/inválido (Pi provisionado) | — |
| `502` | La cámara respondió con error | `CAMERA_ERROR`, `CAMERA_AUTH`, `LIST_FULL` |
| `504` | No se alcanzó la cámara a tiempo | `CAMERA_UNREACHABLE`, `CAMERA_TIMEOUT` |

`LIST_FULL` es explícito a propósito: la cámara tiene un máximo de placas
(`plateListNum` de `/ISAPI/Traffic/capabilities`) y quedarse sin espacio no
debe fallar en silencio.

> El cuerpo de error **nunca** ecoa el request: ni credenciales de cámara, ni
> el bearer. En los logs del Pi solo quedan `requestId`, `action`, `cameraId`,
> `plateNormalized` e `ip`.

**`POST /anpr-event`** — la cámara postea su `EventNotificationAlert`
(multipart con el XML + jpgs, o XML crudo). El Pi extrae placa, fecha de
captura, confianza y `UUID`, y lo encola en SQLite (`anpr-events.db`). Un
thread los reenvía **en orden** a `POST {api}/aditum-gate/anpr-events` con el
token del dispositivo, con backoff de 5 s a 5 min; Aditum deduplica por
`eventUid`, así que un reintento tras timeout no duplica bitácora. Los
eventos confirmados se purgan a los `anpr.purgeDays` días.

Es el **único** endpoint público además de `/` y el editor: la cámara no sabe
mandar bearer. Se acota filtrando la IP de origen (ver `anpr.cameras` abajo), y
por lo que hace: solo encolar. Un heartbeat o un evento sin placa responde
`200 {"ignored": true}` para que la cámara no reintente.

**Configuración en el editor local** (`/admin` → Hikvision → *Cámaras ANPR*),
que se guarda en `anpr.cameras` del documento de configuración:

```json
"anpr": {
  "enabled": true,
  "purgeDays": 7,
  "cameras": [{ "ip": "192.168.68.64", "gateId": 7, "name": "Entrada principal" }]
}
```

Cada cámara declarada cumple **dos** funciones: es la allowlist de quién puede
postear en `/anpr-event`, y dice a qué **portón** pertenecen sus lecturas. Ese
`gateId` viaja en el evento y el backend lo valida contra los gates de este
mismo dispositivo (si no cuadra lo descarta), así la bitácora registra la
puerta correcta. Con la lista vacía se acepta cualquier IP privada de la LAN y
las lecturas quedan sin portón — es el estado de un equipo recién instalado.

> **No hace falta ningún token de la cámara.** La autenticación Pi→Aditum es el
> token de dispositivo que ya está provisionado (`PUT /token`), y la URL de
> destino es la `api.baseUrl` que el equipo ya tiene configurada. El token AES
> `ANPR*{companyId}*{gateId}` del flujo legacy desaparece: existía solo porque
> la cámara le hablaba directo al servidor.

> El filtro por IP depende de que nginx pase la IP real: el `location =
> /anpr-event` del template setea `X-Forwarded-For $remote_addr` — con
> `$proxy_add_x_forwarded_for` una cámara podría anteponer una IP falsa a la
> real y saltarse el filtro. Sin ese header, Flask vería `127.0.0.1` en todos
> los eventos (filtro inútil con allowlist vacía, y `403` a todo con
> allowlist configurada).

**Configuración de la cámara** (`Event → Alarm Setting → Alarm Server`) — el
cutover del piloto es exactamente este cambio, y el rollback es revertirlo:

| Campo | Antes (flujo legacy) | Ahora (local-first) |
|---|---|---|
| Destination IP or Host Name | `caseta.aditumcr.com` | **IP local del Pi** (ej. `192.168.68.100`) |
| URL | `/api/aditum-gate-plate-reading/{TOKEN_AES}` | `/anpr-event` |
| Protocol Type | `HTTPS` | `HTTP` |
| Port No. | `443` | `80` |
| ANR | ✅ | ✅ (dejarlo: cubre que el Pi esté caído; la cola del Pi cubre que lo esté internet) |

Ya no hace falta token AES en la URL: el evento no viaja por internet, y el
que lo reenvía a Aditum es el Pi con su token de dispositivo. El endpoint
legacy `/api/aditum-gate-plate-reading/{token}` queda intacto en el backend
durante toda la transición, así que una cámara sin migrar sigue funcionando.

### Pantalla (compatibilidad) y mantenimiento

| Endpoint | Método | Notas |
|---|---|---|
| `/code-accepted/<name>` | GET | Reenvía el estado a la pantalla local y enciende el LED verde 4 s (si hay NeoPixel) |
| `/code-denied/<name>` | GET | ídem con LED rojo 4 s |
| `/wait-for-response/<name>` | GET | ídem con LED amarillo parpadeante hasta el veredicto |
| `/restart` | POST | Reinicia el proceso (PM2 lo relanza) — `{"message": "Restarting"}` |

## Provisión y rotación del token

**Provisión inicial** (cualquiera de las dos):
1. *Manual*: el técnico escribe `device-token.txt` en el Pi al instalar.
2. *Remota (TOFU)*: apenas se registra el Entry Point en el admin, el backend
   genera el token y hace `PUT /token` (el Pi sin provisionar lo acepta).
   Hacerlo inmediatamente: mientras no haya token el API queda abierto
   (cualquiera que alcance el puerto puede operarlo); provisionar es lo que
   activa el enforcement.

**Rotación en dos fases** (responsabilidad del backend — el Pi mantiene un
solo token vigente): al rotar, guardar `device_token_hash` (viejo) y
`pending_token_hash` (nuevo); hacer `PUT /token`; aceptar ambos hashes hasta
confirmar (próximo `GET /status` exitoso con el nuevo token) y recién
entonces descartar el viejo. Esto cubre el caso del `200` perdido en el
túnel. Rotar ante cualquier sospecha de filtración y al desvincular personal.

**Desprovisionar** (`DELETE /token`): al desvincular o resetear un equipo.
Borra el token con efecto inmediato y reabre la provisión TOFU —
re-provisionar de inmediato o dejar el equipo desvinculado a propósito.

## Modelo de amenaza

- **El Entry Point registrado en Aditum debe ser siempre la URL del túnel
  remoteiot, nunca una IP/puerto forwardeado del router del condominio**
  (eso enviaría el token en claro por internet).
- Token único por dispositivo: el radio de daño de una filtración es un Pi,
  y se cierra rotando (`PUT /token`) o revocando (`DELETE /token`).
- El backend guarda solo SHA-256 de los tokens; el token nunca va al browser.
- Fuera de alcance deliberadamente (no sobre-ingeniar para este despliegue):
  mTLS, firma HMAC por request, anti-replay con nonce.
- Hardening opcional en el Pi: `ufw` limitando :8080 al túnel y localhost.

## Modelo de datos sugerido en aditum-jh

En la entidad `gate_access` (una por Raspberry):

| Columna | Tipo | Notas |
|---|---|---|
| `device_id` | varchar, unique | ej. `GATE-CR-0034` |
| `entry_point_url` | varchar | URL del túnel (base del API) |
| `device_token_hash` | varchar | SHA-256 del token vigente |
| `pending_token_hash` | varchar null | rotación en dos fases |
| `company_id` | FK opcional | condominio |
| `config_json` | clob | documento completo del schema (lo que se pushea) |
| `config_revision` | int | se incrementa en cada guardado |
| `config_backup` | clob | último `GET /backup` (restaurable tal cual) |
| `backed_up_at` | timestamp | cuándo se tomó el respaldo |
| `last_seen_at` | timestamp | actualizar en cada llamada exitosa |

Pantalla de administración sugerida: editor del JSON validado contra
`config.schema.json`, botón **Aplicar** (`PUT /config` vía servidor),
botones provisionar/rotar/quitar token, botón **Abrir editor del equipo**
(link a `{entry_point_url}/admin`, pestaña nueva), botón **Descargar
respaldo** (`config_backup` como archivo `.json`), badges `provisioned` /
`configSource` / revisión aplicada / last seen.
