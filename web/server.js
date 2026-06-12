/**
 * Servidor de pantalla pedestal (puerto 3000).
 *
 * - Sirve el build de Angular (pedestal-app/dist) — ya no hay ng serve en 4200.
 * - Puente WebSocket: los scanners (device/) postean estados aquí y se
 *   broadcastean a la pantalla.
 * - GET /api/config: subset seguro de config-runtime.json para Angular.
 * - Si la config cambia en disco, emite { state: "reload" } para que la
 *   pantalla se recargue.
 * - Lanza chromium en kiosko solo si la config dice hasScreen.
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

const server = app.listen(PORT, () => {
    console.log(`Servidor de pantalla en puerto ${PORT}`);
    const config = readConfig();
    if (config.screen && config.screen.hasScreen) {
        launchKiosk();
    } else {
        console.log('hasScreen=false: no se lanza el kiosko');
    }
});

// ------------------------------------------------------------------
// WebSocket hacia la pantalla
// ------------------------------------------------------------------
const wss = new WebSocket.Server({ server });
let clients = [];

wss.on('connection', (ws) => {
    clients.push(ws);
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

// ------------------------------------------------------------------
// Kiosko
// ------------------------------------------------------------------
function launchKiosk() {
    const command = 'chromium-browser --start-fullscreen ' +
        '--disable-session-crashed-bubble --incognito http://localhost:3000';
    exec(command, { env: { ...process.env, DISPLAY: process.env.DISPLAY || ':0' } },
        (error) => {
            if (error) {
                console.error(`No se pudo lanzar el kiosko: ${error.message}`);
            }
        });
}
