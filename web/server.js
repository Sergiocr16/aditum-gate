/**
 * Servidor de pantalla pedestal (puerto 3000).
 *
 * - Sirve el build de Angular (pedestal-app/dist) — ya no hay ng serve en 4200.
 * - Puente WebSocket: los scanners (device/) postean estados aquí y se
 *   broadcastean a la pantalla.
 * - GET /api/config: subset seguro de config-runtime.json para Angular.
 * - Si la config cambia en disco, emite { state: "reload" } para que la
 *   pantalla se recargue.
 * - Kiosko supervisado (rol del viejo aditum-screen-web): chromium a
 *   pantalla completa si la config dice hasScreen; se relanza si muere
 *   y se cierra si hasScreen pasa a false (sin reiniciar procesos).
 */
const express = require('express');
const bodyParser = require('body-parser');
const cors = require('cors');
const WebSocket = require('ws');
const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');

const PORT = 3000;
const REPO_ROOT = path.join(__dirname, '..');
const RUNTIME_CONFIG = path.join(REPO_ROOT, 'config-runtime.json');
const DEFAULT_CONFIG = path.join(REPO_ROOT, 'config-default.json');
const ANGULAR_DIST = path.join(__dirname, 'pedestal-app', 'dist', 'pedestal-app', 'browser');

function readConfig() {
    for (const file of [RUNTIME_CONFIG, DEFAULT_CONFIG]) {
        try {
            if (fs.existsSync(file)) {
                return JSON.parse(fs.readFileSync(file, 'utf8'));
            }
        } catch (error) {
            console.error(`Config ${file} ilegible: ${error.message}`);
        }
    }
    return {};
}

const app = express();
app.use(bodyParser.json());
app.use(cors());

// Subset seguro de la config para la pantalla (nunca credenciales/token)
app.get('/api/config', (req, res) => {
    const config = readConfig();
    res.json({
        deviceId: config.deviceId || '',
        placeName: config.placeName || '',
        screen: config.screen || { hasScreen: false },
    });
});

if (fs.existsSync(ANGULAR_DIST)) {
    app.use(express.static(ANGULAR_DIST));
} else {
    console.warn(`No existe el build de Angular en ${ANGULAR_DIST} — ` +
        'correr "npm run build" en web/pedestal-app');
}

// Al arrancar este server (deploy por self-update, reinicio de PM2) la
// pantalla conectada sigue corriendo el bundle viejo en memoria: se le
// pide recargar para que tome el build nuevo. El delay da tiempo a que
// el WebSocket de la pantalla se reconecte (reintenta cada 3 s).
const RELOAD_ON_BOOT_MS = 8000;

const server = app.listen(PORT, () => {
    console.log(`Servidor de pantalla en puerto ${PORT}`);
    superviseKiosk();
    setTimeout(() => broadcastState({ state: 'reload' }), RELOAD_ON_BOOT_MS);
});

// ------------------------------------------------------------------
// WebSocket hacia la pantalla
// ------------------------------------------------------------------
const wss = new WebSocket.Server({ server });
let clients = [];

// Ultimo heartbeat de lectores del proceso device (null hasta el primero)
let readerStatus = null;

wss.on('connection', (ws) => {
    clients.push(ws);
    if (readerStatus !== null) {
        ws.send(JSON.stringify({ readerOk: readerStatus }));
    }
    ws.on('close', () => {
        clients = clients.filter((client) => client !== ws);
    });
});

const broadcastState = (data) => {
    const dataString = JSON.stringify(data);
    clients.forEach((client) => {
        if (client.readyState === WebSocket.OPEN) {
            client.send(dataString);
        }
    });
};

// Recargar la pantalla cuando el config-agent escribe una config nueva
fs.watchFile(RUNTIME_CONFIG, { interval: 5000 }, () => {
    console.log('config-runtime.json cambió: recargando pantalla');
    broadcastState({ state: 'reload' });
});

// ------------------------------------------------------------------
// Estados que postean los scanners / el dispositivo
// ------------------------------------------------------------------
app.post('/api/code-accepted', (req, res) => {
    const newState = { state: 2, ...req.body };
    broadcastState(newState);
    res.json({ message: 'Código aceptado', state: newState });
    setTimeout(() => broadcastState({ state: 1 }), 5000);
});

app.post('/api/loading', (req, res) => {
    const newState = { state: 6, ...req.body };
    broadcastState(newState);
    res.json({ message: 'Cargando', state: newState });
});

app.post('/api/code-denied', (req, res) => {
    const newState = { state: 4, ...req.body };
    broadcastState(newState);
    res.json({ message: 'Código no leído', state: newState });
    setTimeout(() => broadcastState({ state: 1 }), 4000);
});

app.post('/api/wait-for-response', (req, res) => {
    const newState = { state: 3, ...req.body };
    broadcastState(newState);
    res.json({ message: 'Esperando respuesta', state: newState });
});

app.post('/api/success-exit', (req, res) => {
    broadcastState({ state: 5 });
    res.json({ message: 'Success exit', state: 5 });
});

// El proceso device avisa que arranco (POST /restart del admin, config
// nueva): se recarga la pantalla para que tome build y config frescos
app.post('/api/reload', (req, res) => {
    broadcastState({ state: 'reload' });
    res.json({ message: 'Pantalla recargada' });
});

// Heartbeat de lectores (device/aditum_gate/screen.py, cada 15 s): la
// pantalla muestra el indicador "Lector QR activo" del pie con esto. El
// mensaje WS { readerOk } es aparte de los de estado { state }.
app.post('/api/reader-status', (req, res) => {
    readerStatus = !!(req.body && req.body.ok);
    broadcastState({ readerOk: readerStatus });
    res.json({ message: 'Estado del lector', readerOk: readerStatus });
});

// ------------------------------------------------------------------
// Kiosko (rol del viejo aditum-screen-web): chromium a pantalla completa
// contra este mismo server. Supervisado: si chromium muere o el escritorio
// todavia no estaba listo al boot, se reintenta; si hasScreen pasa a false
// en la config, se cierra. PM2 nos corre como root, asi que el navegador
// se lanza como el usuario duenio de la sesion grafica (X11 o Wayland).
// ------------------------------------------------------------------
const KIOSK_URL = 'http://localhost:3000';
// [c]hromium: regex que matchea el proceso pero no al propio pgrep/pkill
const KIOSK_PATTERN = '[c]hromium.*localhost:3000';
const KIOSK_CHECK_MS = 15000;
const KIOSK_FLAGS = '--start-fullscreen --disable-session-crashed-bubble ' +
    '--noerrdialogs --no-first-run --incognito';
let kioskLaunching = false;

function desktopUser() {
    // Duenio de la sesion grafica: el primer /run/user/<uid> no-root con
    // entrada en /etc/passwd. En una Pi de escritorio es "pi" (uid 1000).
    try {
        const uids = fs.readdirSync('/run/user')
            .filter((u) => /^\d+$/.test(u) && u !== '0')
            .sort((a, b) => Number(a) - Number(b));
        const passwd = fs.readFileSync('/etc/passwd', 'utf8').split('\n');
        for (const uid of uids) {
            const entry = passwd.find((l) => l.split(':')[2] === uid);
            if (entry) {
                const parts = entry.split(':');
                return { name: parts[0], uid, home: parts[5] };
            }
        }
    } catch (error) { /* sin sesion grafica todavia */ }
    return null;
}

function kioskCommand() {
    // Binario segun la version de Raspberry Pi OS
    const chromium = 'BROWSER=$(command -v chromium-browser || command -v chromium)';
    const run = `$BROWSER ${KIOSK_FLAGS} ${KIOSK_URL}`;

    if (process.getuid && process.getuid() === 0) {
        const user = desktopUser();
        if (!user) return null; // el supervisor reintenta en el proximo ciclo
        const rtdir = `/run/user/${user.uid}`;
        const env = [`DISPLAY=:0`, `XDG_RUNTIME_DIR=${rtdir}`];
        try {
            // Wayland (Bookworm default): el socket se llama wayland-N
            const wl = fs.readdirSync(rtdir).find((f) => /^wayland-\d+$/.test(f));
            if (wl) env.push(`WAYLAND_DISPLAY=${wl}`);
        } catch (error) { /* X11 puro */ }
        if (fs.existsSync(path.join(user.home, '.Xauthority'))) {
            env.push(`XAUTHORITY=${path.join(user.home, '.Xauthority')}`);
        }
        return `runuser -u ${user.name} -- sh -c '${chromium}; ${env.join(' ')} ${run}'`;
    }
    // En dev (sin root) se lanza directo con el entorno propio
    return `sh -c '${chromium}; ${run}'`;
}

function launchKiosk() {
    if (kioskLaunching) return;
    const command = kioskCommand();
    if (!command) {
        console.log('Kiosko: sin sesion grafica todavia, se reintenta');
        return;
    }
    kioskLaunching = true;
    console.log('Lanzando kiosko de pantalla');
    // exec resuelve cuando chromium TERMINA: el flag evita relanzar en paralelo
    exec(command, (error) => {
        kioskLaunching = false;
        if (error) {
            console.error(`Kiosko termino con error: ${error.message}`);
        }
    });
}

function superviseKiosk() {
    const tick = () => {
        const config = readConfig();
        const wantsKiosk = !!(config.screen && config.screen.hasScreen);
        exec(`pgrep -f "${KIOSK_PATTERN}"`, (err) => {
            const running = !err;
            if (wantsKiosk && !running && !kioskLaunching) {
                launchKiosk();
            } else if (!wantsKiosk && running) {
                console.log('hasScreen=false: cerrando el kiosko');
                exec(`pkill -f "${KIOSK_PATTERN}"`);
            }
        });
    };
    tick();
    setInterval(tick, KIOSK_CHECK_MS);
}
