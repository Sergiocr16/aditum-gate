# API del dispositivo Aditum Gate

Contrato del API HTTP que expone cada Raspberry Pi (Flask, puerto **8080**)
para que el backend de Aditum (aditum-jh) lo administre. La URL base de cada
dispositivo es su **Entry Point** registrado en Aditum (la URL del túnel
remoteiot — ver [Modelo de amenaza](#modelo-de-amenaza)).

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
> Un Pi **sin token provisionado** NO deja el API abierto: exige la sesión
> del editor en línea para todo, salvo `PUT /token` (provisión TOFU), y se
> delata en `GET /status` con `"provisioned": false`. La meta es que ningún
> Pi quede en ese estado.

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
    {"name": "aditum-device", "online": true, "pid": 2305,
     "status": "online", "restarts": 0, "uptimeSec": 5400},
    {"name": "aditum-web", "online": true, "pid": 2307,
     "status": "online", "restarts": 0, "uptimeSec": 5400},
    {"name": "web-server-3000", "online": true, "httpStatus": 200}
  ],
  "pm2Available": true,
  "inputDevices": [
    {"path": "/dev/input/event4", "name": "Newtologic  4010E"}
  ],
  "readers": [
    {"role": "entry", "doorId": "34", "deviceName": "Newtologic  4010E",
     "connected": true, "paths": ["/dev/input/event4"]}
  ],
  "system": {"cpuTempC": 52.1, "uptimeSec": 86400,
             "memAvailableMb": 512, "memTotalMb": 944},
  "kioskExpected": false
}
```
- `web-server-3000` es un check HTTP real contra `:3000`: PM2 puede reportar
  el proceso `online` con el puerto muerto; este campo distingue ambos casos.
- `pm2Available: false` → no se pudo consultar PM2 (p.ej. banco de dev);
  los servicios traen solo lo verificable (`online` por check directo).
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
pollear `GET /` hasta obtener 200, luego `GET /status` y confirmar que
`configRevision` es la nueva (si no lo es, la config cacheada falló al
cargar y el Pi cayó al default — alertar). Serializar los pushes: **un
`PUT /config` en vuelo por dispositivo a la vez**.

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
TOFU: `GET /status` reporta `provisioned: false` y `PUT /token` acepta un
token nuevo sin credencial — **re-provisionar de inmediato**, igual que en
la instalación. El backend la usa al desvincular o resetear un equipo.
Firmware viejo sin este endpoint responde `405` (con credencial válida) o
`401` (sin ella).

### Portones

| Endpoint | Método | Respuesta |
|---|---|---|
| `/openGate/<id>` | GET | `{"id": 1, "status": 0}` — pulso de apertura (1 s) |
| `/closeGate/<id>` | GET | `{"id": 1, "status": 0}` |
| `/gateStatus` | GET | `[{"id":1,"pin":7,"status":0}, ...]` |
| `/gateStatus/<id>` | GET | `{"value": 1}` |

Portón inexistente → `404 {"error": "..."}`.

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

### Pantalla (compatibilidad) y mantenimiento

| Endpoint | Método | Notas |
|---|---|---|
| `/code-accepted/<name>` | GET | Reenvía el estado a la pantalla local |
| `/code-denied/<name>` | GET | ídem |
| `/wait-for-response/<name>` | GET | ídem |
| `/restart` | POST | Reinicia el proceso (PM2 lo relanza) — `{"message": "Restarting"}` |

## Provisión y rotación del token

**Provisión inicial** (cualquiera de las dos):
1. *Manual*: el técnico escribe `device-token.txt` en el Pi al instalar.
2. *Remota (TOFU)*: apenas se registra el Entry Point en el admin, el backend
   genera el token y hace `PUT /token` (el Pi sin provisionar lo acepta).
   Hacerlo inmediatamente: mientras no haya token, el backend no puede
   administrar el Pi (todo salvo `PUT /token` exige la sesión del editor).

**Rotación en dos fases** (responsabilidad del backend — el Pi mantiene un
solo token vigente): al rotar, guardar `device_token_hash` (viejo) y
`pending_token_hash` (nuevo); hacer `PUT /token`; aceptar ambos hashes hasta
confirmar (próximo `GET /status` exitoso con el nuevo token) y recién
entonces descartar el viejo. Esto cubre el caso del `200` perdido en el
túnel. Rotar ante cualquier sospecha de filtración y al desvincular personal.

## Modelo de amenaza

- **El Entry Point registrado en Aditum debe ser siempre la URL del túnel
  remoteiot, nunca una IP/puerto forwardeado del router del condominio**
  (eso enviaría el token en claro por internet).
- Token único por dispositivo: el radio de daño de una filtración es un Pi,
  y se cierra con `PUT /token`.
- El backend guarda solo SHA-256 de los tokens; el token nunca va al browser.
- Fuera de alcance deliberadamente (no sobre-ingeniar para este despliegue):
  mTLS, firma HMAC por request, anti-replay con nonce.
- Hardening opcional en el Pi: `ufw` limitando :8080 al túnel y localhost.

## Modelo de datos sugerido en aditum-jh

Tabla `gate_device`:

| Columna | Tipo | Notas |
|---|---|---|
| `device_id` | varchar, unique | ej. `GATE-CR-0034` |
| `entry_point_url` | varchar | URL del túnel (base del API) |
| `device_token_hash` | varchar | SHA-256 del token vigente |
| `pending_token_hash` | varchar null | rotación en dos fases |
| `company_id` | FK opcional | condominio |
| `config_json` | clob | documento completo del schema |
| `config_revision` | int | se incrementa en cada guardado |
| `last_seen_at` | timestamp | actualizar en cada llamada exitosa |

Pantalla de administración sugerida: editor del JSON validado contra
`config.schema.json`, botón **Aplicar** (`PUT /config` vía servidor),
botones provisionar/rotar token, badges `provisioned` / `configSource` /
revisión aplicada / last seen.
