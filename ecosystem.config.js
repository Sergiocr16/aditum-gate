/**
 * Procesos del dispositivo, supervisados por PM2 como root.
 *
 *   sudo pm2 startOrRestart ecosystem.config.js && sudo pm2 save
 *
 * Rutas absolutas via __dirname: pm2 save congela rutas en el dump, asi que
 * nada puede depender del cwd de quien invoco pm2. El interprete Python es
 * el venv del repo (.venv/), creado por scripts/bootstrap.sh.
 * aditum-device sale solo cuando se aplica una configuracion nueva
 * (PUT /config o poller) y PM2 lo relanza.
 */
const path = require('path');

module.exports = {
  apps: [
    {
      name: 'aditum-device',
      script: 'device/main.py',
      cwd: __dirname,
      interpreter: path.join(__dirname, '.venv', 'bin', 'python3'),
      autorestart: true,
      restart_delay: 3000,
      kill_timeout: 5000,
      env: { PYTHONUNBUFFERED: '1' },
    },
    {
      name: 'aditum-web',
      script: 'web/server.js',
      cwd: __dirname,
      autorestart: true,
      restart_delay: 3000,
    },
  ],
};
