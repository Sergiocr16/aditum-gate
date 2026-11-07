# Historial y diagnóstico de placas ANPR

Sirve para investigar una placa que **no abrió** o salió como **otherList**
aunque estaba registrada en Aditum. Reconstruye su recorrido por el Pi y la
cámara:

```
Aditum ──/sync-plates──▶ Pi ──ISAPI──▶ lista de la cámara ──▶ lectura ANPR ──▶ ALARM OUT ──▶ aguja
         (requestId)        ADDED/REMOVED     (allowlist?)     (otherList?)    (orden)       (sensor)
```

El contrato completo del endpoint está en [API.md](API.md#historial-de-una-placa-anpr-plate-history).
El registro del carro y sus cambios viven en Aditum. Lo que falta allá para
cruzar ambas puntas está en [anpr-requerimientos-aditum.md](anpr-requerimientos-aditum.md).

## Cómo se consulta

**Desde el editor del equipo** (`http://<pi>/admin` → **ANPR** → *Historial de una
placa* → **Investigar una placa**):
1. Ingresá la placa y, opcionalmente, la IP de la cámara y el rango de fechas.
2. Marcá *Consultar también la cámara en vivo*, con usuario y contraseña, para
   ver si hoy la tiene, su reloj y su registro de apertura.
3. La pantalla muestra el diagnóstico, las cámaras, la línea de tiempo con fotos
   y las lecturas parecidas.

El botón **«¿Qué puede confirmar esta cámara?»** corre la sonda.

**Por API**, desde el backend (con el token del dispositivo) o con una sesión
del editor:

```bash
# Solo historial local (no toca la cámara)
curl -s -X POST http://<pi>/anpr-plate-history -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"plate":"ABC123","cameraIp":"192.168.1.64","from":"2026-11-01","to":"2026-11-06"}'

# + consulta en vivo a la cámara: FIND, reloj y log de ALARM OUT
curl -s -X POST http://<pi>/anpr-plate-history -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"plate":"ABC123","cameraIp":"192.168.1.64",
       "cameras":[{"ip":"192.168.1.64","user":"admin","password":"..."}]}'
```

## Cómo se lee la respuesta

1. **`summary`:** cuándo entró por última vez (`lastAdded`) y cuándo salió
   (`lastRemoved`), con su `requestId`, que es el id de la fila de
   `anpr_plate_sync_queue` en Aditum. También cuántas lecturas hubo y cuántas
   salieron otherList estando en el allow list.
2. **`findings`:** la explicación automática. Los casos que buscábamos:
   - `OTHERLIST_WHILE_ON_ALLOWLIST`: el Pi la había cargado en la cámara y
     aun así la cámara la rechazó. **Falla de la cámara**: vigencia, reloj o
     match. Repetir con `cameras` para ver la fila real y el reloj.
   - `NOT_ON_LIST_AT_READ`: **no estaba** en la lista al leerla. Dice si
     todavía no había entrado (y cuándo entró) o si la habían sacado (cuándo y
     por qué vía: Aditum, editor o un cambio por fuera).
   - `SYNC_FAILED_BEFORE_READ`: falló el envío a la cámara antes de la
     lectura. **Falla en Aditum → Pi → cámara.**
   - `POSSIBLE_MISREAD`: la cámara leyó otra placa parecida (`A8C123` en vez
     de `ABC123`). **Falla del OCR.** Ver las fotos en `similarReads`.
   - `READ_ALLOWLIST_NOT_OPENED`: la reconoció, pero su log no registra la
     salida de alarma. **Falla entre la cámara y la aguja.**
3. **`timeline`:** todo en orden cronológico.
4. **`reads[].photos`:** las fotos de las lecturas no reconocidas (15 días).

## Apertura de la aguja

**Quién abre.** La cámara, sola: tiene configurada una salida de alarma
(ALARM OUT) para las placas del allowlist, y esa salida dispara la barrera. El
Pi no interviene: solo le carga la lista (`/sync-plates`), y `/anpr-event` solo
registra y reenvía las lecturas. Por eso hay tres eventos
distintos, y la respuesta los separa:

| Evento | Evidencia | Disponible |
|---|---|---|
| Placa **reconocida** (allow list) | `reads[].list = allowlist` | Siempre (desde el despliegue) |
| **Orden de apertura** (ALARM OUT activada) | Log de alarmas de la cámara (`/ISAPI/ContentMgmt/logSearch`) ±15 s de la lectura → `openStage: OPEN_ORDER` | Solo con credenciales y **si la cámara tiene SD**: sin almacenamiento responde `invalidMemoryCardOrNAS` (caso de la cámara de prueba) |
| **Apertura física** | Evento IO de un sensor → `anpr_io_event` → `openStage: PHYSICAL` | Solo si se instala un sensor (abajo) |

Otras fuentes revisadas:
- `/ISAPI/System/IO/outputs/1/status`: da el estado **en este instante**. No
  sirve para un pulso de 1 a 5 s ocurrido antes.
- `/ISAPI/Event/triggers/vehicledetection-1`: en algunos firmwares muestra el
  linkage a la salida. En la cámara de prueba (V5.8.61) **no aparece** aunque
  la cámara sí abre. No sirve para concluir que no abre.
- `/ISAPI/Parking/channels/1/barrierGate`: solo en modelos de
  entrada/salida con barrera controlada por la cámara (RS-485).

**Para confirmar la apertura física** hace falta un sensor:
- el contacto "abierta" del controlador de la barrera, un final de carrera o
  el lazo de salida;
- cableado a la **ALARM IN** de la cámara;
- con la notificación de esa entrada al *Alarm Server*, que es el mismo que ya
  apunta al Pi (`/anpr-event`).

El Pi guarda esos eventos y el historial los cruza con cada lectura (de −5 a
+30 s). Alternativa sin tocar la cámara: una entrada GPIO del Pi, que requiere
código adicional.

### Qué dice la cámara de prueba

Validado el 2026-11-06 con `docs/anpr-validacion-camara-real.md`, contra la
cámara de Villanea (`192.168.100.101`), que postea al Pi `192.168.100.223`.
Solo lectura.

| Pregunta | Resultado |
|---|---|
| Modelo / firmware | iDS-2CD7A46G0/P-IZHSY · V5.8.61 build 260327 |
| Reloj | NTP (`ntp.hik-proconnect.com`), UTC-6, desfase ~1 s con el Pi. Las lecturas traen hora local con offset (`-06:00`) |
| ¿Quién abre? | **La cámara**: su salida de alarma está configurada para el allowlist (confirmado en sitio). El Pi solo carga la lista |
| ¿El linkage se ve por ISAPI? | **No.** `Event/triggers/vehicledetection-1` solo muestra `center` (HTTP Listening al Pi). En este firmware ese linkage no se expone, así que la sonda lo informa como "no visible", no como "no abre" |
| Salidas de alarma | 2 en modo pulso: salida 1 de 1 s y salida 2 de 5 s |
| Entradas de alarma | 2 habilitadas y libres, que sirven para un sensor de la barrera |
| ¿SD / almacenamiento? | **No** (`hddList` vacío; soporta NAS, pero no hay ninguno montado) |
| ¿Registro de alarmas (ALARM OUT)? | El firmware **sí** registra `alarmOut`, pero la búsqueda responde `403 invalidMemoryCardOrNAS` **sin SD**. Hoy no se puede consultar |
| ¿Evento IO al abrir? | No: por alertStream solo llega el evento ANPR. La activación de la salida no se notifica |
| Registros de paso en la cámara (`dataOperation`) | No existe en este firmware (404) |
| API de barrera (`Parking/barrierGate`) | No existe (404) |
| `vehicleListName` | `otherList` observado. El código ya acepta `whiteList`/`allowList`/`blockList`/`otherList` |
| Lista de placas | CSV con BOM, `Group` 0 = BlockList y 1 = AllowList, fechas `YYYY-MM-DD`. 85 placas de 10000; el export tardó 216 ms |

**Conclusión: hoy la orden de apertura de esta cámara no se puede confirmar.**
El historial responde con evidencia si la placa estaba en el allowlist y cómo
la leyó la cámara. Para saber si además se activó la salida hay dos caminos,
de menor a mayor esfuerzo:

1. **Poner una microSD en la cámara.** Con almacenamiento, su registro de
   alarmas se puede consultar y trae `alarmOut`. El historial lo cruza solo
   (consulta en vivo, ±15 s): la lectura sale con `opened: true/false`. Es lo
   más simple y no requiere código nuevo.
2. **Sensor de la barrera en una ALARM IN** (hay 2 libres), con su
   notificación al *Alarm Server* del Pi. Confirma la **apertura física**, no
   solo la orden. El Pi ya guarda esos eventos.

Hay una alternativa sin hardware que no está implementada: que el Pi consulte
`/ISAPI/System/IO/outputs/<n>/status` apenas recibe una lectura en allowlist,
mientras dura el pulso (1 a 5 s). Requiere que el Pi guarde las credenciales
de la cámara, y hoy por diseño no lo hace.

## Limitaciones

**Disponible desde el despliegue** (y por `anpr.historyDays`, default 90 días):
- entradas y salidas de cada placa en la lista de cada cámara, con su origen;
- envíos fallidos;
- todas las lecturas con su lista;
- eventos IO.

**Recuperable hoy, de lo anterior al despliegue:**

| Dato | Dónde | Hasta cuándo |
|---|---|---|
| Lecturas allowlist / descartadas / repetidas | `anpr_event` del Pi (`/anpr-status`) | `purgeDays` (7 días) |
| Lecturas no reconocidas con foto | `anpr-captures` del Pi | 15 días / 500 MB |
| Lecturas recibidas por Aditum | `anpr_camera_event` en Aditum | Sin purga |
| Registro y cambios del carro | `bitacora_acciones` en Aditum (tipo 9) | Sin purga |
| Último estado del envío por placa | `anpr_plate_sync_queue` en Aditum | 30 días; se sobreescribe |
| Si hoy está en la cámara y desde cuándo, aproximado | `FIND` (`/anpr-test` o `cameras` en el historial): `validTo` − 20 años = última vez que el Pi escribió esa fila | Estado actual |
| Logs del Pi | `/root/.pm2/logs/aditum-device-*.log` (requiere root) | ~7 × 10 MB |

**No se puede saber:**
- **antes del despliegue**, si la placa estaba en la lista de la cámara en un
  momento dado;
- la **apertura física** sin un sensor;
- la orden de apertura si la cámara no tiene SD (su log no se puede
  consultar) o si el firmware no registra ALARM OUT;
- el **momento exacto de un cambio hecho por fuera** (`EXTERNAL`): se detecta
  en la siguiente escritura del Pi en esa cámara (a lo sumo, el full sync
  nocturno);
- las fotos de lecturas en **allow list**: la cámara las manda, pero no se
  guardan;
- a qué placa corresponde una **lectura ilegible** (`unknown`): no tiene
  placa; está en `/anpr-captures` por hora, con foto.

Además:
- `capturedAt` es la hora de la cámara. Con el reloj corrido o una lectura
  reenviada tarde (ANR) aparece `CLOCK_SKEW` y `onAllowlistAtCaptureTime`.
- El historial es **por Pi**: si una cámara pasa a otro Pi, su historial queda
  en el anterior.
