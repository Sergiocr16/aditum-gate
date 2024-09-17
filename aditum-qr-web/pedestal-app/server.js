const express = require('express');
const bodyParser = require('body-parser');
const cors = require('cors');
const WebSocket = require('ws');

const app = express();
const PORT = 3000;

app.use(bodyParser.json());
app.use(cors());

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
