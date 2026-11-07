# Runbook: documentar los formatos ISAPI de una cámara ANPR real

> **Para un agente (Claude Code) corriendo en una Raspberry Pi con acceso de
> red a una cámara Hikvision ANPR real.** Es autocontenido: solo usa `curl`.
> No hace falta que el Pi tenga ninguna versión particular de aditum-gate.

## Contexto

En el repo **aditum-gate** se implementó un diagnóstico de placas ANPR
(`POST /anpr-plate-history` y la sonda `POST /anpr-plate-history/probe`). Una
parte consulta a la cámara para responder:

- qué hora tiene (las lecturas traen la hora de la cámara);
- si hoy tiene una placa en su allow list;
- si **registró la orden de apertura** (salida de alarma, *ALARM OUT*) cuando
  reconoció una placa;
- si está **configurada** para abrir sola con el allow list (*linkage*);
- si puede reportar una **apertura física** (sensor en su *ALARM IN*).

El problema: **los formatos de esas respuestas ISAPI no se verificaron contra
hardware real**, y varían por modelo y firmware. Tu trabajo es capturar los
formatos reales de esta cámara y devolver un reporte, para ajustar el código
con datos y no con supuestos.

Contexto operativo:
- La cámara **abre la aguja sola**: reconoce la placa, la busca en su allow
  list y, por linkage, activa su salida de alarma (ALARM OUT), que dispara la
  barrera.
- El Pi recibe las lecturas por *HTTP Listening / Alarm Server* en
  `POST /anpr-event`, pero no participa de la apertura.

## ⛔ Reglas de seguridad (obligatorias)

Es una cámara **en producción** que controla un acceso real.

1. **Solo lectura.** Prohibido cualquier `PUT`, o un `POST` que no sea de
   búsqueda. En particular **NUNCA**:
   - `PUT /ISAPI/System/IO/outputs/<n>/trigger`: **abre la aguja**;
   - `PUT .../licensePlateAuditData`: **reemplaza la lista de placas** y puede
     dejar a todo el condominio sin acceso;
   - cambiar Alarm Server, linkage, NTP, hora, usuarios ni nada de
     configuración;
   - reiniciar la cámara.

   Los únicos `POST` permitidos son las búsquedas: `ContentMgmt/logSearch` y
   `Traffic/ContentMgmt/dataOperation` con `operationType=search`.
2. **Credenciales.**
   - Pedíselas al usuario; no las adivines.
   - Hikvision **bloquea el usuario tras varios intentos fallidos**. Si te da
     `401` dos veces, **pará** y pedí que las verifique.
   - Usá variables de entorno, nunca escribas la contraseña en archivos, en el
     reporte ni en commits.
3. **`alertStream`.** Es solo lectura, pero no lo dejes abierto más de
   10 minutos, y una sola conexión a la vez.
4. **Datos.**
   - Guardá todo en `~/anpr-camara-<fecha>/`, **fuera** de cualquier repo git.
   - En el reporte, enmascará las placas: dos primeros caracteres + `***`.
   - Reemplazá `serialNumber` y `macAddress` por `***`.

## Preparación

```bash
# Pedile al usuario IP, usuario y contraseña de la cámara
export CAM_IP='192.168.x.x'   CAM_USER='admin'   CAM_PASS='...'
OUT=~/anpr-camara-$(date +%Y%m%d); mkdir -p "$OUT"; cd "$OUT"
isapi() {  # isapi <archivo> <METODO> <path> [archivo-body]
  local f=$1 m=$2 p=$3 b=$4
  if [ -n "$b" ]; then
    curl -s --digest -u "$CAM_USER:$CAM_PASS" --max-time 40 -X "$m" \
      -H 'Content-Type: application/xml' --data-binary @"$b" \
      -o "$f" -w "$m $p -> %{http_code} (%{time_total}s)\n" "http://$CAM_IP$p"
  else
    curl -s --digest -u "$CAM_USER:$CAM_PASS" --max-time 40 -X "$m" \
      -o "$f" -w "$m $p -> %{http_code} (%{time_total}s)\n" "http://$CAM_IP$p"
  fi
}
```

Primer contacto (si da `401`, pará y verificá credenciales):

```bash
isapi 00-deviceInfo.xml GET /ISAPI/System/deviceInfo && cat 00-deviceInfo.xml
```

## Pasos

### 1. Identidad y reloj

```bash
isapi 01-time.xml GET /ISAPI/System/time; date -Iseconds; cat 01-time.xml
isapi 01-ntp.xml  GET /ISAPI/System/time/ntpServers
```

Anotá:
- modelo y firmware (`model`, `firmwareVersion`, `firmwareReleasedDate`);
- `localTime` exacto (con o sin zona, y qué zona);
- `timeMode` y `timeZone`;
- **desfase** contra `date` del Pi, en segundos.

### 2. Lista de placas (SOLO export)

```bash
isapi 02-plates.csv GET '/ISAPI/Traffic/channels/1/licensePlateAuditData?fileType=csv'
head -c 3 02-plates.csv | xxd | head -1      # ¿trae BOM UTF-8 (ef bb bf)?
head -1 02-plates.csv; sed -n '2,4p' 02-plates.csv; wc -l 02-plates.csv
isapi 02-traffic-cap.xml GET /ISAPI/Traffic/capabilities; grep -o '<plateListNum>[^<]*' 02-traffic-cap.xml
```

Anotá:
- el header exacto y el orden de columnas;
- el formato de fechas (`Start Date`/`End Date`);
- qué valores tiene `Group` (0, 1, otros);
- la cantidad de placas y la capacidad;
- cuánto tardó el export.

### 3. Linkage: ¿la cámara está configurada para abrir con el allow list?

```bash
isapi 03-triggers.xml GET /ISAPI/Event/triggers
isapi 03-trigger-vd.xml GET /ISAPI/Event/triggers/vehicledetection-1
isapi 03-event-cap.xml GET /ISAPI/Event/capabilities
grep -n -i 'vehicle\|anpr\|plate\|outputIOPortID\|notificationMethod\|list' 03-triggers.xml | head -60
```

Si `vehicledetection-1` da 404, buscá en `03-triggers.xml` y en
`/ISAPI/Traffic/capabilities` el nombre que use este firmware. Pueden servir
`/ISAPI/Traffic/channels/1/vehicleDetect` y sus subrutas, siempre solo con
`GET`.

Anotá:
- **el path y el fragmento XML exacto** donde se ve que la detección de
  vehículo, para el allow list, dispara la salida de alarma
  (`notificationMethod=IO`, `outputIOPortID`…);
- si el linkage es **por tipo de lista** (allow/block/other) o general.

> Si no aparece, **no concluyas que la cámara no abre**: en el firmware V5.8.61
> el linkage allowlist → salida de alarma está configurado y la cámara abre,
> pero este trigger solo muestra `center`. Anotalo como "no visible por ISAPI"
> y preguntale al usuario cómo está configurada.

### 4. Salidas y entradas de alarma

```bash
for p in capabilities outputs outputs/1 outputs/1/status inputs inputs/1 inputs/1/status; do
  isapi "04-io-${p//\//_}.xml" GET "/ISAPI/System/IO/$p"
done
```

**NO** llames a `outputs/1/trigger`. Anotá:
- cuántas salidas y entradas hay;
- duración del pulso (`PulseDuration` o similar) y estado por defecto;
- cómo se reporta el estado (`ioState`: active/inactive);
- si alguna entrada está configurada y qué la dispara.

### 5. Log de la cámara (lo más importante)

Objetivo: saber si la cámara **registra la activación de la salida de alarma**,
con qué texto y con qué formato de hora. El código actual manda esto:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
<searchID>8a3f1c2e-0000-4000-8000-000000000001</searchID>
<metaId>log.std-cgi.com/Alarm</metaId>
<timeSpanList><timeSpan>
<startTime>2026-11-06T00:00:00Z</startTime>
<endTime>2026-11-06T23:59:59Z</endTime>
</timeSpan></timeSpanList>
<maxResults>100</maxResults>
<searchResultPostion>0</searchResultPostion>
</CMSearchDescription>
```

Las horas van en hora **local** con una `Z` (convención Hikvision, que suele
ignorar la Z), y el código busca en el tipo de cada entrada el texto
`alarm out`.

Probá **todas** estas combinaciones para las últimas 24 h y anotá cuáles
responden `200` con resultados:

```bash
isapi 05-log-cap.xml GET /ISAPI/ContentMgmt/logSearch/capabilities
D=$(date +%Y-%m-%d); Y=$(date -d yesterday +%Y-%m-%d)
for meta in 'log.std-cgi.com' 'log.std-cgi.com/Alarm' 'log.hikvision.com' 'log.hikvision.com/Alarm'; do
  for fmt in Z offset local; do
    case $fmt in Z) S="${Y}T$(date +%H:%M:%S)Z"; E="${D}T$(date +%H:%M:%S)Z";;
      offset) S="${Y}T$(date +%H:%M:%S)$(date +%:z)"; E="${D}T$(date +%H:%M:%S)$(date +%:z)";;
      local) S="${Y}T$(date +%H:%M:%S)"; E="${D}T$(date +%H:%M:%S)";; esac
    n="05-log-${meta//[\/.]/_}-$fmt"
    printf '<?xml version="1.0" encoding="UTF-8"?><CMSearchDescription><searchID>%s</searchID><metaId>%s</metaId><timeSpanList><timeSpan><startTime>%s</startTime><endTime>%s</endTime></timeSpan></timeSpanList><maxResults>100</maxResults><searchResultPostion>0</searchResultPostion></CMSearchDescription>' \
      "$(cat /proc/sys/kernel/random/uuid)" "$meta" "$S" "$E" > "$n.req.xml"
    isapi "$n.xml" POST /ISAPI/ContentMgmt/logSearch "$n.req.xml"
  done
done
grep -l 'searchMatchItem' 05-log-*.xml
```

Con la combinación que funcione:
- listá los **tipos distintos** de entrada con su cantidad (`metaId` /
  `majorType` / `minorType`, o los tags que use);
- copiá **3 `searchMatchItem` completos** al reporte, uno de ellos de alarma
  si existe;
- identificá **el texto exacto** con que aparece la activación de la salida
  de alarma: "Alarm Output Start", "alarmOutStart", "Alarm Out"…;
- anotá los nombres de los tags de hora y de puerto;
- revisá la paginación (`responseStatusStrg`: OK/MORE/NO MATCHES) y el
  máximo por página.

Si nada responde en XML, probá
`POST /ISAPI/ContentMgmt/logSearch?format=json` y documentá el formato que
pida el error.

### 6. Eventos en vivo y correlación con la apertura

Pedile al usuario que haga pasar un vehículo **autorizado** (en allow list)
durante la captura. Si se puede, también uno **no autorizado**. Si no hay
coordinación, esperá tráfico real.

```bash
timeout 600 curl -s -N --digest -u "$CAM_USER:$CAM_PASS" \
  "http://$CAM_IP/ISAPI/Event/notification/alertStream" > 06-alertstream.txt
grep -c '<eventType>' 06-alertstream.txt; grep -o '<eventType>[^<]*' 06-alertstream.txt | sort | uniq -c
```

Las imágenes vienen en partes binarias del multipart: solo interesa el XML.

1. Copiá **un `EventNotificationAlert` de ANPR completo** al reporte, con la
   placa enmascarada. Anotá:
   - los valores reales de `vehicleListName` (whiteList/allowList/otherList…);
   - `direction`, `dateTime` (formato y zona), `picNum`, `UUID`;
   - cualquier tag que hable de alarma, IO, barrera o salida.
2. ¿Aparece algún evento **distinto de ANPR** cuando se abre la aguja
   (`eventType` IO, alarmOutput, …)? Si sí, copiá uno completo.
3. Inmediatamente después, repetí la búsqueda del **paso 5** solo para esa
   ventana (±5 min). ¿Hay una entrada de salida de alarma? ¿Con cuántos
   segundos de diferencia respecto del `dateTime` del evento ANPR?
   - Con eso se sabe si la hora del log es local o UTC.
   - También se sabe si ±15 s alcanza para cruzarlas.

### 7. Registros guardados en la cámara (SD)

```bash
isapi 07-storage.xml GET /ISAPI/ContentMgmt/Storage; cat 07-storage.xml | head -30
cat > 07-dataop.req.xml <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<DataOperation><operationType>search</operationType><searchCond>
<searchID>$(cat /proc/sys/kernel/random/uuid)</searchID>
<timeSpanList><timeSpan><startTime>$(date -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)</startTime><endTime>$(date +%Y-%m-%dT%H:%M:%SZ)</endTime></timeSpan></timeSpanList>
<criteria><dataType>0</dataType></criteria>
<searchResultPosition>0</searchResultPosition><maxResults>20</maxResults>
</searchCond></DataOperation>
EOF
isapi 07-dataop.xml POST /ISAPI/Traffic/ContentMgmt/dataOperation 07-dataop.req.xml; head -c 1500 07-dataop.xml
```

Anotá si hay SD y si la búsqueda de pasos funciona. Si responde un error de
formato, copiá el mensaje: suele decir qué campo falta.

### 8. API de barrera (modelos de parqueo)

```bash
isapi 08-barrier.xml GET /ISAPI/Parking/channels/1/barrierGate
isapi 08-barrier-status.xml GET /ISAPI/Parking/channels/1/barrierGate/barrierGateStatus
```

### 9. (Opcional) Sonda de aditum-gate

Solo si este Pi corre la rama `feature/anpr-historial-placas` de aditum-gate.
Hoy esa rama no está publicada: si no la tenés, saltá este paso.

```bash
curl -s -X POST http://localhost:8080/anpr-plate-history/probe -H "Authorization: Bearer $(cat <repo>/device-token.txt)" \
  -H 'Content-Type: application/json' -d "{\"ip\":\"$CAM_IP\",\"user\":\"$CAM_USER\",\"password\":\"$CAM_PASS\"}" > 09-probe.json
```

Compará `09-probe.json` (`summary`) con lo que encontraste a mano.

## Entregable: `~/anpr-camara-<fecha>/REPORTE.md`

Usá exactamente esta estructura:

```markdown
# Cámara ANPR — <modelo> / <firmware> — <fecha>

## 1. Equipo
- Modelo / firmware / fecha de firmware:
- Hora de la cámara vs Pi: <localTime> vs <date> → desfase <n> s; timeMode/timeZone/NTP:

## 2. Respuestas
| Pregunta | Sí/No | Evidencia (archivo + dato) |
|---|---|---|
| ¿Linkage allow list → salida de alarma configurado? | | |
| ¿El linkage es por tipo de lista? | | |
| ¿La búsqueda en el log funciona? (metaId + formato de hora que funcionó) | | |
| ¿El log registra la activación de la salida de alarma? (texto exacto) | | |
| ¿La hora del log es local o UTC? | | |
| Diferencia entre lectura ANPR y entrada de ALARM OUT (s) | | |
| ¿Llega algún evento IO/alarma por alertStream al abrir? | | |
| ¿Tiene entradas de alarma libres para un sensor de barrera? | | |
| ¿Tiene SD y funciona la búsqueda de pasos (dataOperation)? | | |
| ¿Tiene API de barrera (Parking/barrierGate)? | | |
| Valores de vehicleListName observados | | |

## 3. Formatos exactos (XML/CSV reales, enmascarados)
### 3.1 logSearch: request que funcionó + 3 searchMatchItem + tipos distintos con conteo
### 3.2 Linkage: path + fragmento XML
### 3.3 IO: outputs, outputs/1/status, inputs
### 3.4 Evento ANPR completo (alertStream)
### 3.5 Evento IO/alarma (si apareció)
### 3.6 CSV de placas: header, formato de fechas, grupos
### 3.7 dataOperation / Storage / barrierGate: status + extracto

## 4. Ajustes que necesita aditum-gate
Para cada punto: valor actual del código → valor real observado.
- device/aditum_gate/anpr_camera_diag.py
  - `_log_search_body`: metaId (`log.std-cgi.com/Alarm`) y formato de hora (`%Y-%m-%dT%H:%M:%SZ`, hora local)
  - `_ALARM_OUT` (regex `alarm\s*out`) y `_ALARM_IN`: ¿matchean el texto real?
  - `_log_entries`: tags de hora (`StartDateTime|startTime|time|dateTime`), tipo (`metaId|majorType|minorType|logType`) y puerto (`paraType|alarmOutPort|outputIOPortID`)
  - `_camera_dt`: ¿la Z del log es local o UTC?
  - `_PROBE_GETS`: paths que dieron 404 y los paths correctos de este firmware; campos a extraer
- device/aditum_gate/anpr_events.py
  - `IO_EVENT_TYPES` (`io, alarmin, alarminput, alarmout, alarmoutput`) y `_IO_PORT_TAGS`: vs el evento IO real
  - `parse_event_xml`: ¿falta algún campo útil del evento ANPR real?
- device/aditum_gate/anpr_history.py
  - `ALARM_OUT_WINDOW` (±15 s) e `IO_WINDOW` (−5..+30 s): ¿alcanzan según la diferencia medida?

## 5. Archivos crudos
Lista de archivos en ~/anpr-camara-<fecha>/ (sin credenciales).
```

Al terminar:
- verificá que **ningún archivo** contenga la contraseña:
  `grep -rl "$CAM_PASS" ~/anpr-camara-*`, que debe salir vacío;
- hacé `unset CAM_PASS`;
- entregale `REPORTE.md` al usuario.
