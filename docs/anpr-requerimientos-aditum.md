# Requerimientos en aditum-jh: diagnóstico de placas ANPR

> Documento autocontenido para implementar en el repo **aditum-jh** (Spring
> Boot 2.7, JHipster 4.11, AngularJS). Complementa el endpoint
> `POST /anpr-plate-history` que ya existe en el Pi (repo aditum-gate,
> contrato en `docs/API.md`, sección *Historial de una placa*).

## Objetivo

Que Aditum pueda consultar una placa y armar en **una sola respuesta** su
recorrido completo, para saber en qué etapa falló:

1. **Aditum:** registro del carro, modificaciones, habilitado o
   deshabilitado, si era elegible para la cámara, cuándo se encoló y cuándo
   se despachó.
2. **Pi:** cuándo entró y salió de la lista de la cámara, envíos fallidos,
   cada lectura y en qué lista, si estaba en el allow list en ese momento,
   evidencia de apertura y lecturas parecidas.

El caso a resolver: una placa leída como **otherList** que estaba registrada
en Aditum y, en teoría, en el allow list de la cámara.

## Lo que ya existe y se reutiliza

| Dato | Dónde |
|---|---|
| Registro y cambios del carro | `bitacora_acciones` tipo 9, categoría "Vehículos", `id_reference = vehicule.id` (`VehiculeService`: alta, habilitar/deshabilitar, modificación, borrado, carga masiva) |
| Estado actual del carro | `vehicule` (`licenseplate`, `enabled`, `deleted`, `jhi_type`, `last_edited_date`, `house_id`, `company_id`) |
| Placas que se mandan a la cámara | `VehiculeRepository.findActiveLicensePlatesByCompanyIdIn` + el scope de compañías de `AnprPlateSyncDispatchService` (excluye motos, deshabilitados, borrados y sin consentimiento macro cuando aplica) |
| Último envío por placa | `anpr_plate_sync_queue` (`requested_at`, `applied`, `applied_at`, `apply_result`, `apply_attempts`; único por `camera_id` + `plate_normalized`) |
| Lecturas recibidas | `anpr_camera_event` (`license_plate_normalized`, `captured_at`, `received_at`, `vehicle_found`, `gate_access_id`) |
| Cámara y su Pi | `anpr_camera` (`ip`, `login_user`, `login_password`, `gate_access_id`, `company_id`) → `gate_access.entry_point` + token del dispositivo (`GateDeviceTokenService`) |
| Normalización de placa | `LicensePlateUtil.normalize` (igual que el Pi: `[A-Z0-9]`, mayúsculas) |

Correlación clave: el `requestId` que Aditum manda en `/sync-plates` es el id
de la fila de `anpr_plate_sync_queue`. El Pi lo guarda en cada entrada y salida
de la lista (`listHistory[].requestId`), así que una entrada en la cámara se
puede rastrear hasta el envío de Aditum que la originó.

## 1. Endpoint nuevo (obligatorio)

`GET /api/anpr-cameras/{id}/plate-history?plate=ABC-123&from=2026-11-01&to=2026-11-06`

- `@PreAuthorize("hasAuthority('ROLE_ADMIN')")`, igual que el resto de
  `AnprCameraResource`.
- Validar que la cámara pertenece a la compañía o macro del usuario: nunca
  exponer datos de otro condominio.
- `plate` se normaliza con `LicensePlateUtil.normalize`. `from`/`to` son
  opcionales (default: últimos 30 días, zona America/Costa_Rica).

**Servicio** (`AnprPlateHistoryService`, nuevo), con estos pasos:

1. **Lado Aditum**, en solo lectura y sin tocar el Pi:
   - **Carros con esa placa** dentro del scope de la cámara (misma lógica de
     compañías que el dispatcher), incluidos los borrados (`deleted=1`):
     casa, tipo, `enabled`, `deleted`, `last_edited_date`.
   - **Historial del carro**: filas de `bitacora_acciones` con
     `jhi_type=9` e `id_reference` = id del carro, ordenadas por
     `ejecution_date`, mapeadas a eventos `VEHICLE_CREATED`, `VEHICLE_UPDATED`,
     `VEHICLE_ENABLED`, `VEHICLE_DISABLED`, `VEHICLE_DELETED`, `BULK_LOAD`
     según el `concept`. Incluir el responsable (`id_responsable` → nombre).
   - **Elegibilidad hoy**: si la placa está en el snapshot que se mandaría a
     esta cámara. Si no, la razón:
     - `MOTORCYCLE`;
     - `DISABLED`;
     - `DELETED`;
     - `NO_MACRO_CONSENT`;
     - `OUT_OF_SCOPE`, si el carro es de una filial que la política de
       compartir no incluye.
   - **Cola**: la fila de `anpr_plate_sync_queue` para (`camera_id`,
     `plate_normalized`): acción, `requested_at`, `applied_at`,
     `apply_result`, `apply_attempts`.
   - **Lecturas que recibió Aditum**: `anpr_camera_event` con ese
     `license_plate_normalized` y `gate_access_id` de la cámara, en el rango.
2. **Lado Pi**:
   - `POST {gateAccess.entryPoint}/anpr-plate-history` con
     `Authorization: Bearer <token del dispositivo>` y el body:

     ```json
     { "plate": "ABC123", "cameraId": 12, "from": "...", "to": "...",
       "cameras": [ { "ip": "<anpr_camera.ip>", "user": "<login_user>", "password": "<login_password>" } ] }
     ```

   - Usar un `RestTemplate` con **read timeout ≥ 60 s**. El Pi consulta la
     cámara en vivo con un presupuesto de 45 s. El `deviceRestTemplate` de 5 s
     no sirve.
   - Si el Pi no responde (caído, sin la versión nueva, `404` sin ANPR),
     devolver igual el lado Aditum con `device: { "error": "..." }`. Nunca
     fallar la consulta entera.
   - No loguear el body: lleva credenciales.
3. **Fusión**:
   - una sola `timeline` ordenada por fecha con `origin: "ADITUM" | "PI"`;
   - los `findings` del Pi tal cual;
   - los **hallazgos cruzados** de abajo.

**Hallazgos cruzados**, que solo se pueden calcular en Aditum:

| Código | Condición |
|---|---|
| `REGISTERED_BUT_NOT_ELIGIBLE` | El carro está registrado pero no entra en el snapshot. Va con la razón: moto, deshabilitado, sin consentimiento… |
| `QUEUED_NOT_APPLIED` | La fila de la cola sigue `applied=0` o agotó intentos (`apply_attempts` ≥ máximo) |
| `APPLIED_BUT_NOT_ON_CAMERA` | La cola dice aplicado (`OK_FULL_SYNC*`) en T, pero el Pi no tiene un `ADDED` de esa placa en esa cámara después de T |
| `SYNC_DELAY` | Entre el alta o habilitación del carro y el `ADDED` en el Pi pasaron más de N minutos (configurable, sugerido 15) |
| `READ_NOT_RECEIVED_BY_ADITUM` | El Pi tiene una lectura en allow list que no está en `anpr_camera_event` (por `eventUid`) |

**Respuesta** (DTO `AnprPlateHistoryDTO`):

```json
{ "plate": "ABC123", "camera": { "id": 12, "name": "Entrada", "ip": "..." },
  "aditum": { "vehicles": [ ... ], "vehicleHistory": [ ... ], "eligibility": { "eligible": true, "reason": null },
              "queue": { ... }, "receivedEvents": [ ... ] },
  "device": { ...respuesta de /anpr-plate-history del Pi... } | { "error": "PI_UNREACHABLE" },
  "findings": [ ... ], "timeline": [ ... ] }
```

## 2. Cambios de persistencia (recomendados, en orden de valor)

1. **No perder la placa vieja al editar.** Hoy "Modificación de los datos del
   vehículo" en `bitacora_acciones` solo guarda la placa nueva. Al cambiar la
   placa, el `concept` debe decir `placa anterior → nueva`, para que el
   historial se pueda buscar por la placa vieja. Es un cambio de texto, sin
   migración.
2. **Log de despachos** `anpr_plate_dispatch_log` (Liquibase nuevo; registrar
   en `master.xml`). Una fila por cada `POST /sync-plates`:

   | Columna | Valor |
   |---|---|
   | `camera_id` | id de la cámara |
   | `gate_access_id` | id del acceso |
   | `request_id` | el que se mandó al Pi |
   | `sent_at` | fecha del envío |
   | `duration_ms` | duración |
   | `plate_count` | cantidad de placas |
   | `http_status` | código HTTP |
   | `apply_result` | resultado |
   | `pi_added`, `pi_removed`, `pi_external` | del body de respuesta del Pi (ver §4) |

   - Retención de 180 días.
   - Hoy `anpr_plate_sync_queue` se sobrescribe por placa y se purga a los 30
     días. Sin este log, un `requestId` viejo no se puede rastrear.
3. **`anpr_camera_event`**: agregar `vehicle_list` (allowlist / otherlist) y
   `source_ip`, y aceptar `vehicleList` y `sourceIp` en
   `AnprCameraEventRequestDTO`. El Pi ya los tiene y los empezará a mandar
   cuando Aditum los acepte (coordinar; Jackson ignora campos desconocidos,
   pero confirmar que el DTO no lo impida). Con eso la Bitácora de placas
   puede mostrar los otherList sin pasar por el Pi.

## 3. Huecos conocidos que explican "registrada pero no estaba en la cámara"

- **Cambio de consentimiento macro**: `HouseMacroConsentService` no encola
  (`enqueueFullSyncForMacroConsentChange` sin llamadas, TODO TAR-1014). La
  placa recién habilitada por consentimiento solo llega a la cámara en el full
  sync nocturno de las 03:05. Encolar un `FULL_SYNC` de las cámaras afectadas
  al cambiar el consentimiento.
- **Motos excluidas**: `jhi_type` distinto de automóvil nunca se manda. Si un
  carro quedó mal tipificado, nunca entra. El diagnóstico lo muestra como
  `REGISTERED_BUT_NOT_ELIGIBLE / MOTORCYCLE`.
- **El endpoint legacy** `/api/aditum-gate-plate-reading/{token}` solo
  escribe en `bitacora_acciones`, no en `anpr_camera_event`. Las cámaras sin
  migrar no aparecen en el diagnóstico del lado Aditum.

## 4. Respuesta del Pi a `/sync-plates` (ya disponible)

Cada elemento de `results` trae, además de `ip`/`ok`/`detail`, cuántas placas
cambiaron en la cámara con ese envío. Es aditivo: el resto del body no cambió.

```json
{ "ok": true, "plates": 2656, "applied": 1, "failed": 0,
  "results": [ { "ip": "192.168.1.64", "ok": true, "detail": "replaced_2656",
                 "added": 3, "removed": 1, "external": 0 } ] }
```

- `external > 0`: la cámara tenía placas distintas a lo último que escribió el
  Pi (alguien la tocó por fuera o se reseteó). Conviene mostrarlo como alerta.
- El detalle de cada placa (cuál entró o salió, con este `requestId`) queda en
  el historial del Pi y se consulta con `/anpr-plate-history`.

## 5. UI (opcional)

Un botón **"Diagnóstico de placa"** en la pantalla de Cámaras ANPR y en la
Bitácora de placas:
- abre un modal con un input de placa y rango;
- muestra la `timeline` con colores por etapa, usando las clases `ad-*` y la
  paleta del proyecto;
- muestra los `findings` arriba;
- muestra las fotos desde el Pi. Las URL `/anpr-captures/...` son del Pi, así
  que Aditum debe proxyarlas con el token: no exponer el token al navegador.

## Criterios de aceptación

- Consultar una placa de una cámara devuelve, en una respuesta:
  - el alta y los cambios del carro (Aditum);
  - su elegibilidad;
  - el estado de la cola;
  - cuándo entró y salió de la lista de la cámara (Pi);
  - cada lectura con su lista y si estaba en el allow list;
  - la evidencia de apertura disponible.
- Con el Pi caído, la respuesta igual trae el lado Aditum y un
  `device.error`.
- Ningún dato de otra compañía; credenciales de cámara nunca en logs ni en
  la respuesta.
- Tests de servicio con el Pi mockeado (respuesta ok, timeout, 404) y de
  cada hallazgo cruzado.
