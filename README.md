# aditum-gate
Se instala raspberrian en micro sd

1. Instalar Acceso remoto
ejecutamos sudo apt-get update
instalamos en la terminal de raspberry sudo apt-get installl xrdp
ejecutamos ifconfig en la terminal -> y buscamos wlan0 -> inet adress 
descargamos remote desktop y en pc name ponemos ese ip y listo.
sudo connectd_installer
creamos un nuevo device en https://remoteiot.com/ user partners@aditumcr.com
y corremos ese comando que nos dan

2. Instalar node en raspberry
ejecutamos hostname -I para obtener ip de la raspberry
Conectamos mediante shh la macbook, ejecutando shh pi@ip de la raspberry 
Instalamos nvm con el comando 
```curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.35.3/install.sh | bash```
seguido ejecutamos 
```exec bash``` y luego 
```nvm install 12```, y luego ```nvm use 12```
Instalamos express 
npm install express --save
Instalamos python sudo apt install python3 idle3

Nos vamos a visual studio code e instalamos el plugin de shh remote, pones shh pi@ip  y el password, tenemos acceso ahi y vamos a la carpeta pi/home, 
instalamos git. con  sudo apt install git
copiamos el repo del codigo ejecutando git clone https://github.com/Sergiocr16/aditum-gate

Vamos al folder y ejecutamos npm install


3. Servidor corriendo siempre

npm -g install pm2

ejecutamos para que corra el server en pm2. 
pm2 start --name aditum-gate index.js
ejecutamos esto  
pm2 startup systemd para que inicie al iniciar el raspberry pi, nos desplegara un comando como este. sudo env PATH=$PATH:/home/pi/.nvm/versions/node/v12.22.6/bin /home/pi/.nvm/versions/node/v12.22.6/lib/node_modules/pm2/bin/pm2 startup systemd -u pi --hp /home/pi. , lo copiamos , pegamos en el terminal y despues ejecutamos pm2 save


4. Instalamos sudo apt install nginx


ejecutamos cd /etc/nginx/sites-available/
sudo rm default
ejecutamos sudo nano express-aditum-gate
en ese archivo pegamos


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

escribirlo a mano*****

guardamos con ctrl x. , le damos enter 
ejecutamos sudo nginx -t para probar que este bien
vamos a cd /etc/nginx/sites-enabled/
ejecutamos sudo rm default
ejecutamos sudo ln -s /etc/nginx/sites-available/express-aditum-gate /etc/nginx/sites-enabled/express-aditum-gate

ejecutamos sudo systemctl restart nginx

5. Agregamos un firewall
ejecutamos sudo apt install ufw
sudo ufw allow ssh
sudo ufw allow 'Nginx HTTP'
sudo ufw enable


6. Port forwarding
instalamos
sudo apt install connectd

sudo connectd_installer
creamos un nuevo device en https://remoteiot.com/ user partners@aditumcr.com
y corremos ese comando que nos dan


7. QR CODE

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










