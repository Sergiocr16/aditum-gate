const express = require('express');
const bodyParser = require('body-parser');
const cors = require('cors');
const WebSocket = require('ws');
const { spawn, exec } = require('child_process');
const http = require('http');


const app = express();
const PORT = 3000;

app.use(bodyParser.json());
app.use(cors());

let url = "http://localhost:4200";

// Define a function to check the server status
function checkServerStatus() {
    http.get(url, (res) => {
        if (res.statusCode === 200) {
            console.log('Server is up and running!');
            runScreenWeb(); // Call your desired function
            return; // Exit the function to stop the loop
        } else {
            console.log(`Server returned status ${res.statusCode}, retrying...`);
            // Retry after 5 seconds
            setTimeout(checkServerStatus, 5000);
        }
    }).on('error', (error) => {
        console.log(`Error connecting to server: ${error.message}, retrying...`);
        // Retry after 5 seconds
        setTimeout(checkServerStatus, 5000);
    });
}

// Start checking the server status
checkServerStatus();

function runScreenWeb() {
    return new Promise((resolve, reject) => {
        exec('chromium-browser --start-fullscreen --disable-session-crashed-bubble --incognito http://localhost:4200', { cwd: './' }, (error, stdout, stderr) => {
            if (error) {
                console.error(`exec error: ${error}`);
                reject(error);
                return;
            }
            if (stderr) {
                console.error(`stderr: ${stderr}`);
            }
            console.log(`stdout: ${stdout}`);
            resolve(stdout);
        });
    });
}


const server = app.listen(PORT, () => {
    console.log(`Servidor ejecutándose en el puerto ${PORT}`);
    
});




const wss = new WebSocket.Server({ server });

let clients = [];

wss.on('connection', (ws) => {
    console.log('Cliente conectado');
    clients.push(ws);

    ws.on('close', () => {
        console.log('Cliente desconectado');
        clients = clients.filter(client => client !== ws);
    });
});
const broadcastState = (data) => {
    clients.forEach(client => {
        if (client.readyState === WebSocket.OPEN) {
            const dataString = JSON.stringify(data);
            console.log('Broadcasting state:', dataString); 
            client.send(dataString); 
        }
    });
};

app.post('/api/code-accepted', (req, res) => {
    const gateEntryDTO = req.body;
    const waitState = { state: 3, ...gateEntryDTO };
    broadcastState(waitState);
        console.log('Sent state 3:', waitState);
    setTimeout(() => {
        const newState = { state: 2, ...gateEntryDTO }; 
        broadcastState(newState);
        res.json({ message: 'Código aceptado', state: newState });

        setTimeout(() => {
            broadcastState({ state: 1 });
        }, 4000);
    }, 3000); // Retraso simulado de 3 segundos
});


app.post('/api/code-denied', (req, res) => {
    const gateEntryDTO = req.body; 
    const newState = { state: 4, ...gateEntryDTO }; 
    broadcastState(newState);
    res.json({ message: 'Código no leído', state: newState });
    setTimeout(() => {
        broadcastState({ state: 1 }); // Regresa al estado 1 después de 2 segundos
    }, 2000);
});

app.post('/api/wait-for-response', (req, res) => {
    const gateEntryDTO = req.body; 
    const newState = { state: 3, ...gateEntryDTO }; 
    broadcastState(newState);
    res.json({ message: 'Esperando respuesta', state: newState });
});
