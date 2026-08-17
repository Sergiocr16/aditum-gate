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
    // Politica anti-502: PM2 no se rinde nunca. Con el default
    // (max_restarts=16, min_uptime=1s) un crash-loop deja el proceso
    // "errored" y el equipo muerto detras de nginx hasta la pasada de
    // reparacion del timer (15 min). Backoff exponencial con tope 15 s:
    // un loop permanente reintenta suave; uno transitorio se cura solo.
    {
      name: 'aditum-device',
      script: 'device/main.py',
      cwd: __dirname,
      interpreter: path.join(__dirname, '.venv', 'bin', 'python3'),
      autorestart: true,
      min_uptime: 10000,
      max_restarts: 1000000,
      exp_backoff_restart_delay: 500,
      kill_timeout: 5000,
      max_memory_restart: '500M',
      env: { PYTHONUNBUFFERED: '1' },
    },
    {
      name: 'aditum-web',
      script: 'web/server.js',
      cwd: __dirname,
      autorestart: true,
      min_uptime: 10000,
      max_restarts: 1000000,
      exp_backoff_restart_delay: 500,
      max_memory_restart: '300M',
    },
  ],
};
