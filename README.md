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
```
5. Acceder a la carpeta del proyecto clonado y ejecutar el siguiente comando para instalar las dependencias:
 ```
cd aditum-gate
```
```
npm install
```
```
cd aditum-qr-web
```
```
npm install
```
```
cd pedestal-app
```
```
npm install
```
6. Instalar PM2 globalmente mediante el siguiente comando:
 ```
sudo npm -g install pm2
```
7. Ejecutar el siguiente comando para que el servidor se ejecute en PM2:
Para aditum-gate
```
pm2 start --name aditum-gate index.js
```
Para aditum-screen-server
```
pm2 start --name aditum-screen-server server.js
```
Para aditum-screen-web
```
pm2 start npm --name aditum-screen-web -- start
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

Para instalar QR CODE, ejecute los siguientes comandos en el terminal:
```
sudo rm /usr/lib/python3.*/EXTERNALLY-MANAGED
```
```
sudo apt-get install libhdf5-dev
```
```
sudo apt-get install libhdf5-serial-dev 
```
```
sudo apt-get install libatlas-base-dev
```
```
sudo apt-get install libjasper-dev
```
```
sudo apt-get install libqtgui4 
```
```
sudo apt-get install libqt4-test
```
```
pip install numpy==1.25.0
```
```
pip3 install opencv-contrib-python
```
```
sudo apt install python3-opencv
```
```
sudo pip3 install pyzbar
```
```
sudo pip3 install imutils
```
```
sudo pip3 install argparse
```
```
sudo apt-get install uhubctl
```

16. Para controlar LUZ LED
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

17. Nunca apagar la pantalla
```
sudo apt-get install xscreensaver
```
Una vez instalado, vaya al "Menú" del escritorio de Rpi (esquina superior izquierda)
Vaya a preferencias --> salvapantallas.
Verá un menú principal del protector de pantalla. En el menú desplegable de modo, seleccione "deshabilitar protector de pantalla" y luego cierre la ventana.
LISTO.

## Configuración

En aditum-gate/serverGPIO.py si es un raspberry que controla un pedestal entonces:
```
 'isScreen = True;'
```
Si es un raspberry que controla los relays entonces
 'isScreen = False;'


En aditum-gate/scanner.py
```
 doorType = "entry"  # Set this to "exit" or "entry"
 doorId = '0'  # Assign the correct ID based on the type of door
 placeName = 'Test'
 ```

En aditum-qr-web/pedestal-app/src/app/app.component.ts
Definir en la pantalla de pedestal si es de entrada o de salida y el logo del cliente
```
  doorType: string = 'EXIT'; // ENTRY = entrada, EXIT = salida
  clientLogoUrl: string = 'https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg';// Mensaje por defecto
```


# Instalación del reinicio automático cada 10 minutos
## 1. Permitir reinicio de Nginx sin pedir contraseña

Ejecutar este comando una sola vez:

```bash
echo 'pi ALL=(root) NOPASSWD: /bin/systemctl restart nginx' | sudo tee /etc/sudoers.d/nginx-restart
sudo chmod 440 /etc/sudoers.d/nginx-restart
```

Esto permite que el usuario `pi` pueda reiniciar `nginx` desde el cron sin que pida contraseña.

---

## 2. Abrir el crontab

Ejecutar:

```bash
crontab -e
```

---

## 3. Agregar la tarea al final del archivo

Pegar esta línea al final del crontab:

```bash
*/10 * * * * /usr/local/bin/pm2 restart aditum-gate >> /home/pi/restart-services.log 2>&1
*/10 * * * * /usr/local/bin/pm2 restart aditum-screen-web >> /home/pi/restart-services.log 2>&1
*/10 * * * * /usr/local/bin/pm2 restart aditum-screen-server >> /home/pi/restart-services.log 2>&1
*/10 * * * * sudo /bin/systemctl restart nginx >> /home/pi/restart-services.log 2>&1
```

Esta línea reinicia:

- `aditum-gate` con PM2
- `nginx` con systemctl

Cada 10 minutos.

---

## 4. Revisar que el cron quedó guardado

Ejecutar:

```bash
crontab -l
```

Deberías ver una línea como esta:

```bash
*/10 * * * * /usr/local/bin/pm2 restart aditum-gate >> /home/pi/restart-services.log 2>&1 && sudo /bin/systemctl restart nginx >> /home/pi/restart-services.log 2>&1
```

---

## 5. Probar manualmente el comando

Antes de esperar al cron, se puede probar manualmente con:

```bash
/usr/local/bin/pm2 restart aditum-gate >> /home/pi/restart-services.log 2>&1 && sudo /bin/systemctl restart nginx >> /home/pi/restart-services.log 2>&1
```

---

## 6. Ver el log

Para revisar si se ejecutó correctamente:

```bash
tail -f /home/pi/restart-services.log
```

---

## Nota importante

Si `pm2` no funciona desde cron, revisar la ruta exacta con:

```bash
which pm2
```

Y reemplazar `/usr/local/bin/pm2` por la ruta que devuelva ese comando.
