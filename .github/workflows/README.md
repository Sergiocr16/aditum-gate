# GitHub Actions - Automatic Deployment

Este directorio contiene los workflows de GitHub Actions para el despliegue automático del proyecto.

## Workflows Disponibles

### 1. Deploy to Development (`deploy-development.yml`)

Se ejecuta automáticamente cuando:
- Se hace push o merge a la rama `development`
- Se ejecuta manualmente desde la pestaña "Actions" de GitHub

### 2. Deploy to Production (`deploy-production.yml`)

Se ejecuta automáticamente cuando:
- Se hace push o merge a la rama `production`
- Se ejecuta manualmente desde la pestaña "Actions" de GitHub

## Configuración de Secrets

Para que los workflows funcionen, necesitas configurar los siguientes secrets en GitHub:

### Para Development

Ve a: `Settings` → `Secrets and variables` → `Actions` → `New repository secret`

1. **DEV_HOST**: Dirección IP o hostname del servidor de desarrollo
   - Ejemplo: `192.168.1.100` o `dev.aditumcr.com`

2. **DEV_USERNAME**: Usuario SSH para el servidor de desarrollo
   - Ejemplo: `pi`

3. **DEV_SSH_KEY**: Clave privada SSH para autenticación
   - Genera una clave SSH: `ssh-keygen -t rsa -b 4096`
   - Copia el contenido de `~/.ssh/id_rsa` (la clave privada)
   - Añade la clave pública (`~/.ssh/id_rsa.pub`) al servidor: `ssh-copy-id pi@DEV_HOST`

4. **DEV_SSH_PORT** (opcional): Puerto SSH, por defecto 22
   - Ejemplo: `22`

5. **DEV_DEPLOY_PATH** (opcional): Ruta donde está el proyecto en el servidor
   - Por defecto: `/home/pi/aditum-gate`
   - Ejemplo: `/home/pi/aditum-gate`

### Para Production

Mismos secrets pero con prefijo `PROD_`:

1. **PROD_HOST**: Dirección IP o hostname del servidor de producción
2. **PROD_USERNAME**: Usuario SSH para el servidor de producción
3. **PROD_SSH_KEY**: Clave privada SSH para producción
4. **PROD_SSH_PORT** (opcional): Puerto SSH
5. **PROD_DEPLOY_PATH** (opcional): Ruta del proyecto en producción

## Proceso de Despliegue

Cada workflow realiza los siguientes pasos:

1. ✅ **Checkout del código**: Obtiene el código del repositorio
2. ✅ **Setup Node.js**: Configura Node.js versión 20
3. ✅ **Deploy al servidor**:
   - Se conecta vía SSH al servidor
   - Navega al directorio del proyecto
   - Hace `git pull` de la rama correspondiente
   - Instala/actualiza dependencias de Node.js (`npm install`)
   - Instala dependencias de Python (`pip3 install`)
   - Reinicia los servicios PM2
   - Guarda la configuración de PM2

## Uso

### Despliegue Automático

1. Haz merge de tu código a la rama `development` o `production`
2. El workflow se ejecutará automáticamente
3. Puedes ver el progreso en la pestaña "Actions" de GitHub

### Despliegue Manual

1. Ve a la pestaña "Actions" en GitHub
2. Selecciona el workflow que deseas ejecutar
3. Haz clic en "Run workflow"
4. Selecciona la rama y haz clic en "Run workflow"

## Verificación

Después del despliegue, puedes verificar que todo funciona:

```bash
# En el servidor (vía SSH)
pm2 status
pm2 logs aditum-gate
```

## Troubleshooting

### Error de conexión SSH

- Verifica que los secrets estén configurados correctamente
- Asegúrate de que la clave SSH pública esté en el servidor
- Verifica que el firewall permita conexiones SSH

### Error en git pull

- Verifica que el servidor tenga acceso al repositorio
- Asegúrate de que la rama exista en el repositorio remoto

### Error en npm install

- Verifica que Node.js esté instalado en el servidor
- Revisa los logs del workflow para más detalles

### PM2 no reinicia

- Verifica que PM2 esté instalado: `npm install -g pm2`
- Verifica que el servicio esté corriendo: `pm2 list`