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
> Mientras un Pi **no tiene token provisionado**, acepta llamadas sin header
> (modo de transición para no dejar sin servicio a la flota), pero loguea
> error y se delata en `GET /status` con `"provisioned": false`. La meta es
> que ningún Pi quede en ese estado.

## Autenticación

Token opaco por dispositivo (32 bytes hex recomendado), generado por Aditum.
Se envía en cualquiera de los dos headers:

```
Authorization: Bearer <token>
X-Device-Token: <token>
```

- Respuesta sin/mal token: `401 {"error": "unauthorized"}` (también para
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
datos que muestra y guarda salen de `GET /config` y `PUT /config`, que sí
exigen el token (la página lo pide y lo recuerda en el browser). Útil para
técnicos en sitio sin pasar por el admin de Aditum.

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

### Configuración

El documento de configuración está definido por
[`config.schema.json`](../config.schema.json) (ese archivo ES el contrato:
el editor del admin valida client-side contra él antes de enviar). Ejemplos
por variante en [`examples/`](../examples/).

#### `GET /config` — protegido
```json
{ "config": { ...documento completo... }, "source": "config-runtime.json" }
```

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
| `schemaVersion` no soportada | 409 | `{"error": "unsupported schemaVersion", "supportedSchemaVersion": 1}` |
| No valida contra el schema | 400 | `{"error": "invalid config", "details": ["...mensajes jsonschema..."]}` |

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
   Hacerlo inmediatamente: mientras no haya token, el API está abierto.

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
