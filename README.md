# aditum-gate

aditum-gate es un proyecto que permite el control de acceso a través de una interfaz web. Para su implementación, se requiere instalar Raspberry Pi con Raspbian OS y configurar el acceso remoto y Node.js.

## Instalación del acceso remoto

1. Descargue Raspbian OS e instálelo en una tarjeta micro SD.
2. Actualice la lista de paquetes en la terminal de Raspberry Pi ejecutando el siguiente comando:
```
sudo apt-get update
```
3. Instale XRDP para habilitar el escritorio remoto mediante el siguiente comando:
```
sudo apt-get install xrdp
```
4. Ejecute el comando ifconfig en la terminal de Raspberry Pi y busque la dirección IP asociada a la interfaz inalámbrica (wlan0) en el campo inet address.

5. Descargue un cliente de Escritorio Remoto en su PC y use la dirección IP obtenida en el paso anterior como "PC Name" para conectarse al escritorio remoto.

6. Cree un nuevo dispositivo en https://remoteiot.com/ ejecutando el siguiente comando en la terminal de Raspberry Pi:
```
sudo connectd_installer
```
7. Ingrese con el correo "partners@aditumcr.com" para obtener un token.


## Instalación de Node.js
1. Obtenga la dirección IP de Raspberry Pi ejecutando el siguiente comando en la terminal:
```
hostname -I
```
2. Conéctese a Raspberry Pi a través de SSH desde su Macbook usando el siguiente comando:
```
ssh pi@<IP de Raspberry Pi>
```
3. Instale NVM (Node Version Manager) mediante el siguiente comando:
```
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.35.3/install.sh | bash
```
4. Ejecute el siguiente comando para habilitar NVM en la sesión actual:
```
exec bash
```
5. Instale la versión 12 de Node.js mediante el siguiente comando:
```
nvm install 12
```
6. Use la versión 12 de Node.js instalada mediante el siguiente comando:
```
nvm use 12
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

1. Instalar Visual Studio Code y el plugin de SSH Remote.
2. Conectar a la Raspberry Pi a través de SSH usando la dirección IP y contraseña correspondientes.
3. Una vez dentro de la Raspberry Pi, dirigirse a la carpeta /home/pi y ejecutar el siguiente comando para instalar Git:
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
npm install

```
6. Instalar PM2 globalmente mediante el siguiente comando:
 ```
npm -g install pm2
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

## Configuración del firewall
Para agregar un firewall, ejecute los siguientes comandos en el terminal:
```
sudo apt install ufw
sudo ufw allow ssh
sudo ufw allow 'Nginx HTTP'
sudo ufw enable
```
### Port forwarding
Para instalar el port forwarding, ejecute el siguiente comando en el terminal:
```
sudo apt install connectd
```
Después, cree un nuevo dispositivo en https://remoteiot.com/ y copie el comando que se le brinda.
## Instalación de QR CODE
Para instalar QR CODE, ejecute los siguientes comandos en el terminal:
```
pip install numpy --upgrade
sudo apt-get install libhdf5-dev -y
sudo apt-get install libhdf5-serial-dev –y
sudo apt-get install libatlas-base-dev –y
sudo apt-get install libjasper-dev -y
sudo apt-get install libqtgui4 –y
sudo apt-get install libqt4-test –y
pip3 install opencv-contrib-python==4.1.0.25
pip3 install pyzbar
pip3 install imutils
pip3 install argparse
modprobe bcm283

```

LISTO.








