# aditum-gate

aditum-gate es un proyecto que permite el control de acceso a través de una interfaz web. Para su implementación, se requiere instalar Raspberry Pi con Raspbian OS y configurar el acceso remoto y Node.js.

## Instalación del acceso remoto

1. Descargue Raspbian OS e instálelo en una tarjeta micro SD.
2. Actualice la lista de paquetes en la terminal de Raspberry Pi ejecutando el siguiente comando:
```
sudo apt-get update
```

6. Cree un nuevo dispositivo en https://remoteiot.com/ ejecutando el siguiente comando en la terminal de Raspberry Pi:

7. Ingrese con el correo "partners@aditumcr.com" para obtener un token.

8. Configurar VNC
```
sudo raspi-config
```
Select the 'Advanced options' menu, then 'Wayland', then choose 'X11' and reboot. You can then re-enable VNC and you will have RealVNC working as on previous release of Raspberry Pi OS.

## Instalación de Node.js

3. Instale NVM (Node Version Manager) mediante el siguiente comando:
```
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
```
```
nvm install 20
```
```
exec bash
```
```
nvm use 20
```
```
sudo apt-get install nodejs npm
```
4. Ejecute el siguiente comando para habilitar NVM en la sesión actual:
```
nvm alias default 20
```

7. Instale Express mediante el siguiente comando:
```
npm install express --save
```
8. Instale Python 3 mediante el siguiente comando:
```
sudo apt install python3 idle3
```

## Descarga del repositorio del proyecto y configuración del servidor Node.js

```
sudo apt install git
```
4. Clonar el repositorio del código ejecutando el siguiente comando:
```
git clone https://github.com/Sergiocr16/aditum-gate
git checkout qr-readers
git pull origin
```
5. Acceder a la carpeta del proyecto clonado y ejecutar el siguiente comando para instalar las dependencias:
 ```
cd aditum-gate
```
```
npm install
```
6. Instalar PM2 globalmente mediante el siguiente comando:
 ```
sudo npm -g install pm2
```
7. Ejecutar el siguiente comando para que el servidor se ejecute en PM2:
 ```
pm2 start --name aditum-gate index.js
```
8. Ejecutar el siguiente comando para configurar PM2 para que inicie el servidor automáticamente al iniciar la Raspberry Pi:
 ```
pm2 startup systemd
```
Este comando mostrará un comando similar a este:
 ```
sudo env PATH=$PATH:/home/pi/.nvm/versions/node/v12.22.6/bin /home/pi/.nvm/versions/node/v12.22.6/lib/node_modules/pm2/bin/pm2 startup systemd -u pi --hp /home/pi
```
Copiar el comando y pegarlo en el terminal, y luego ejecutar el siguiente comando para guardar la configuración de inicio de PM2:
 ```
pm2 save
```
9. Instalar Nginx mediante el siguiente comando:
 ```
sudo apt install nginx
```
10. Eliminar el archivo de configuración predeterminado de Nginx ejecutando el siguiente comando:
 ```
cd /etc/nginx/sites-available/
 ```
 ```
sudo rm default
```
11. Crear un nuevo archivo de configuración para el servidor ejecutando el siguiente comando:
 ```
sudo nano express-aditum-gate
```
Dentro del archivo, escribir el siguiente código a mano
 ```
server {
  listen 80;
  server_name _;
  location / {
    proxy_pass http://localhost:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host $host;
    proxy_cache_bypass $http_upgrade;
  }
}
```
Guardar el archivo presionando Ctrl + X, luego Y para confirmar y presionando Enter.

12. Eliminar el archivo de configuración predeterminado de Nginx en sites enabled ejecutando el siguiente comando:
 ```
 cd /etc/nginx/sites-enabled/
 ```
 ```
sudo rm default
```
13. Crear un enlace simbólico al archivo de configuración creado anteriormente ejecutando el siguiente comando:
 ```
sudo ln -s /etc/nginx/sites-available/express-aditum-gate /etc/nginx/sites-enabled/express-aditum-gate
```
14. Ejecutar el siguiente comando para verificar que la configuración de Nginx sea correcta:
 ```
sudo nginx -t
```
15. Reiniciar el servidor Nginx ejecutando el siguiente comando:
 ```
sudo systemctl restart nginx
```
16. Librerias para que los lectores QR funcionen correctamente
 ```
sudo pip3 install evdev
```
18. Para controlar LUZ LED
```
sudo pip3 install rpi_ws281x
```
```
sudo pip3 install adafruit-circuitpython-neopixel
```
```
sudo python3 -m pip install --force-reinstall adafruit-blinka
```

Los puertos de la led se conectan en tierra y el 6to de la derecha para el controlador, luego el 5v de la led va al toma junto con otro tierra que va al otro extremo del toma.

19. Nunca apagar la pantalla
```
sudo apt-get install xscreensaver
```
Una vez instalado, vaya al "Menú" del escritorio de Rpi (esquina superior izquierda)
Vaya a preferencias --> salvapantallas.
Verá un menú principal del protector de pantalla. En el menú desplegable de modo, seleccione "deshabilitar protector de pantalla" y luego cierre la ventana.
LISTO.

## Sistema de Configuración Centralizada

El proyecto utiliza un sistema de configuración centralizada que permite gestionar todos los parámetros desde un servicio externo. El sistema identifica cada dispositivo mediante un ID único y obtiene su configuración automáticamente.

### Estructura de Configuración

La configuración se almacena en un servicio externo y se estructura de la siguiente manera:

```json
{
  "device": {
    "deviceId": "DEVICE-001",
    "deviceName": "Nombre del Dispositivo",
    "scannerType": "qr",
    "scannerScript": "scannerQr.py"
  },
  "hardware": {
    "hasScreen": true,
    "hasTwoCameras": true,
    "isScreen": true,
    "deviceName": "Newtologic  4010E"
  },
  "door": {
    "doorType": "entry",
    "doorId": "0",
    "placeName": "Nombre del Lugar"
  },
  "display": {
    "clientLogoUrl": "https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg",
    "showCameraFeed": true
  },
  "api": {
    "baseUrl": "https://app.aditumcr.com/api"
  },
  "polling": {
    "intervalSeconds": 30,
    "enabled": true
  }
}
```

### Configuración del Dispositivo

#### 1. Identificador del Dispositivo

Cada dispositivo debe tener un identificador único en el archivo `device-id.txt`:

```bash
# Editar el archivo device-id.txt en la raíz del proyecto
nano device-id.txt
```

El contenido debe ser un ID único, por ejemplo:
```
DEVICE-001
```

Este ID se utiliza para identificar el dispositivo cuando se solicita la configuración al servicio externo.

#### 2. Configuración por Defecto

El archivo `config-default.json` contiene los valores por defecto que se utilizarán si:
- El servicio de configuración externo no está disponible
- No existe una configuración en caché
- Es la primera vez que se ejecuta el sistema

Este archivo se encuentra en la raíz del proyecto y puede ser editado directamente si es necesario.

### Servicio de Configuración Externo

El sistema espera que exista un servicio externo que proporcione la configuración mediante una API REST.

#### Endpoint Requerido

El servicio debe proporcionar un endpoint con el siguiente formato:

```
GET {baseDomain}/api/devices/{deviceId}/config
```

Donde:
- `baseDomain`: Se deriva automáticamente de `baseUrl` en la configuración (por ejemplo, si `baseUrl` es `https://app.aditumcr.com/api`, el `baseDomain` será `https://app.aditumcr.com`)
- `deviceId`: El ID del dispositivo leído desde `device-id.txt`

#### Respuesta Esperada

El endpoint debe devolver un objeto JSON con la estructura de configuración descrita anteriormente.

### Funcionamiento del Sistema

1. **Inicialización**: Al iniciar, el sistema:
   - Lee el `device-id.txt` para obtener el ID del dispositivo
   - Intenta obtener la configuración desde el servicio externo
   - Si falla, utiliza la configuración en caché (`config-cache.json`)
   - Si no hay caché, utiliza la configuración por defecto (`config-default.json`)

2. **Polling Automático**: El sistema verifica automáticamente si hay cambios en la configuración:
   - Intervalo configurable (por defecto: 30 segundos)
   - Se puede habilitar/deshabilitar mediante `polling.enabled`
   - Los cambios se detectan automáticamente y se aplican sin reiniciar

3. **Caché Local**: La configuración se guarda localmente en `config-cache.json` para:
   - Funcionar sin conexión si el servicio externo no está disponible
   - Reducir la latencia en accesos repetidos
   - Proporcionar un fallback en caso de errores

### Parámetros de Configuración

#### device
- `deviceId`: ID único del dispositivo (debe coincidir con `device-id.txt`)
- `deviceName`: Nombre descriptivo del dispositivo
- `scannerType`: Tipo de escáner (`"qr"` o `"camera"`)
- `scannerScript`: Script Python a ejecutar (`"scannerQr.py"` o `"scanner.py"`)

#### hardware
- `hasScreen`: Indica si el dispositivo tiene pantalla (para mostrar mensajes)
- `hasTwoCameras`: Indica si hay dos cámaras disponibles (ejecuta scripts en paralelo)
- `isScreen`: Para `serverGPIO.py`, indica si es un Raspberry que controla un pedestal (`true`) o relays (`false`)
- `deviceName`: Nombre del dispositivo de entrada para lectores QR (ej: `"Newtologic  4010E"`)

#### door
- `doorType`: Tipo de puerta (`"entry"` para entrada, `"exit"` para salida)
- `doorId`: ID de la puerta asignado en el sistema
- `placeName`: Nombre del lugar donde está instalado el dispositivo

#### display
- `clientLogoUrl`: URL del logo del cliente a mostrar en la pantalla
- `showCameraFeed`: Indica si se debe mostrar la alimentación de la cámara (solo para `scanner.py`)

#### api
- `baseUrl`: URL base de la API de Aditum (ej: `"https://app.aditumcr.com/api"`)

#### polling
- `intervalSeconds`: Intervalo en segundos entre verificaciones de configuración (por defecto: 30)
- `enabled`: Habilita o deshabilita el polling automático (`true` o `false`)

### Puertos del Sistema

Los puertos del sistema están hardcodeados y no son configurables:

- **Servidor Principal**: `7777` (index.js)
- **Servidor Node.js**: `3000` (aditum-qr-web/server.js)
- **Servidor GPIO**: `8080` (serverGPIO.py)
- **Servidor Angular Dev**: `4200` (pedestal-app)

### Ejemplo de Configuración Manual

Si necesita configurar manualmente sin usar el servicio externo:

1. Edite `config-default.json` con los valores deseados
2. Asegúrese de que `device-id.txt` contenga el ID correcto
3. El sistema utilizará automáticamente estos valores

### Solución de Problemas

**El sistema no obtiene la configuración del servicio externo:**
- Verifique que `device-id.txt` contenga un ID válido
- Verifique la conectividad de red
- Revise que el servicio externo esté disponible
- El sistema utilizará automáticamente la configuración en caché o por defecto

**La configuración no se actualiza:**
- Verifique que `polling.enabled` esté en `true`
- Revise los logs para ver si hay errores en el polling
- Verifique que el servicio externo esté devolviendo la configuración correcta

**Configuración incorrecta:**
- Verifique el formato JSON en `config-default.json` o `config-cache.json`
- Elimine `config-cache.json` para forzar una recarga desde el servicio externo
- Revise los logs del sistema para ver errores específicos

## Despliegue Automático (CI/CD)

El proyecto incluye un sistema de despliegue automático mediante GitHub Actions que actualiza el código automáticamente cuando se hace merge a las ramas `development` o `production`.

### Cómo Funciona

1. **Despliegue a Development**: Cuando se hace merge o push a la rama `development`, el código se actualiza automáticamente en el servidor de desarrollo.

2. **Despliegue a Production**: Cuando se hace merge o push a la rama `production`, el código se actualiza automáticamente en el servidor de producción.

### Configuración Inicial

Para habilitar el despliegue automático, necesitas configurar los secrets de GitHub:

1. Ve a tu repositorio en GitHub
2. Navega a `Settings` → `Secrets and variables` → `Actions`
3. Haz clic en `New repository secret`

#### Secrets para Development

- **DEV_HOST**: IP o hostname del servidor de desarrollo (ej: `192.168.1.100`)
- **DEV_USERNAME**: Usuario SSH (ej: `pi`)
- **DEV_SSH_KEY**: Clave privada SSH para autenticación
- **DEV_SSH_PORT** (opcional): Puerto SSH, por defecto `22`
- **DEV_DEPLOY_PATH** (opcional): Ruta del proyecto, por defecto `/home/pi/aditum-gate`

#### Secrets para Production

- **PROD_HOST**: IP o hostname del servidor de producción
- **PROD_USERNAME**: Usuario SSH
- **PROD_SSH_KEY**: Clave privada SSH
- **PROD_SSH_PORT** (opcional): Puerto SSH
- **PROD_DEPLOY_PATH** (opcional): Ruta del proyecto

### Generar Clave SSH

En tu máquina local, genera una clave SSH si no tienes una:

```bash
ssh-keygen -t rsa -b 4096 -C "github-actions"
```

Luego copia la clave pública al servidor:

```bash
ssh-copy-id pi@DEV_HOST
```

Y copia el contenido de la clave privada (`~/.ssh/id_rsa`) como secret en GitHub.

### Flujo de Trabajo

1. **Desarrollo**: Trabaja en una rama feature y haz merge a `development`
   - El código se desplegará automáticamente al servidor de desarrollo

2. **Producción**: Cuando estés listo, haz merge de `development` a `production`
   - El código se desplegará automáticamente al servidor de producción

### Qué Hace el Despliegue Automático

Cada vez que se hace merge, el sistema:

1. ✅ Se conecta al servidor vía SSH
2. ✅ Actualiza el código desde el repositorio (`git pull`)
3. ✅ Instala/actualiza dependencias de Node.js (`npm install`)
4. ✅ Instala dependencias de Python (`pip3 install`)
5. ✅ Reinicia los servicios PM2
6. ✅ Guarda la configuración de PM2

### Verificar Despliegue

Puedes verificar el estado del despliegue:

1. **En GitHub**: Ve a la pestaña `Actions` para ver el progreso
2. **En el servidor**: Conecta vía SSH y verifica:
   ```bash
   pm2 status
   pm2 logs aditum-gate
   ```

### Despliegue Manual

También puedes ejecutar el despliegue manualmente:

1. Ve a `Actions` en GitHub
2. Selecciona el workflow (`Deploy to Development` o `Deploy to Production`)
3. Haz clic en `Run workflow`
4. Selecciona la rama y ejecuta

### Troubleshooting

**El despliegue falla:**
- Verifica que los secrets estén configurados correctamente
- Asegúrate de que la clave SSH pública esté en el servidor
- Verifica que el servidor tenga acceso a internet para hacer `git pull`
- Revisa los logs en la pestaña `Actions` de GitHub

**El código no se actualiza:**
- Verifica que el merge se haya hecho correctamente
- Revisa que la rama exista en el repositorio remoto
- Verifica los logs del workflow en GitHub Actions

Para más detalles, consulta `.github/workflows/README.md`

## Configuración lectores QR

Para configurar los lectores debe de por cada uno escanearse los siguientes códigos
#Modo lectura continua
![IMG_4926](https://github.com/user-attachments/assets/61b9e8fd-4ada-4b6d-94ec-8b3d92aeeed6)

#Agregar sufijo personalizado
En este caso es un @ , cada vez que lea el @ ejecutará la lectura 
-Habilitar los sufijos personalizados
![IMG_4927](https://github.com/user-attachments/assets/846fb310-255d-46f1-913e-e44ef3c76cba)
- Establecer sufijo personalizado
![IMG_4928](https://github.com/user-attachments/assets/e39e7731-7b37-42bd-b7aa-887dd9d907b7)

-Codigo Hex Para el sufijo @ es 99 40

2 veces 9
 ![IMG_4929](https://github.com/user-attachments/assets/ba37595a-54c0-4031-a5c4-8e32dabd8385)
 40
 ![IMG_4930](https://github.com/user-attachments/assets/6ae4057e-897f-42cb-89c7-17ede227af7d)
 ![IMG_4931](https://github.com/user-attachments/assets/a7b5ea92-b9bd-4fd1-8ca4-71c6dd5a45a6)

-Esperar 2 segundos cada vez que lea un código
![IMG_4932](https://github.com/user-attachments/assets/261ec1de-e5f8-4ad1-8a21-a9657055d3a9)


LISTO.








