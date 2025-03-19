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


Verificar el puerto del lector

sudo evtest


LISTO.








