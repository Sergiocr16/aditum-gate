const app = require("express")();
const { spawn } = require('child_process');

const qrCodeReader = true;

let serverPyProcess = null;  // Proceso para serverGPIO.py
let scannerPyProcess = null; // Proceso para scanner.py

// Función para ejecutar el script scanner.py usando spawn
function runScannerPy() {
    if (scannerPyProcess) {
        console.log('Deteniendo proceso anterior de scanner.py...');
        scannerPyProcess.kill(); // Mata el proceso anterior

        // Esperar 1 segundo antes de reiniciar el proceso
        setTimeout(() => {
            startScannerProcess();
        }, 1000);
    } else {
        startScannerProcess(); // Ejecutar si no hay un proceso previo
    }
}

function startScannerProcess() {
    scannerPyProcess = spawn('python', ['./scanner.py']);

    // Gestionar la salida del proceso
    scannerPyProcess.stdout.on('data', (data) => {
        console.log(`Salida de scanner.py: ${data}`);
    });

    scannerPyProcess.stderr.on('data', (data) => {
        console.error(`Error en scanner.py: ${data}`);
    });

    scannerPyProcess.on('close', (code) => {
        console.log(`Proceso scanner.py terminó con código ${code}`);
    });
}

// Función para ejecutar el script serverGPIO.py usando spawn
function runServerPy() {
    if (serverPyProcess) {
        console.log('Deteniendo proceso anterior de serverGPIO.py...');
        serverPyProcess.kill(); // Mata el proceso anterior

        // Esperar 1 segundo antes de reiniciar el proceso
        setTimeout(() => {
            startServerProcess();
        }, 1000);
    } else {
        startServerProcess(); // Ejecutar si no hay un proceso previo
    }
}

function startServerProcess() {
    serverPyProcess = spawn('python', ['./serverGPIO.py']);

    // Gestionar la salida del proceso
    serverPyProcess.stdout.on('data', (data) => {
        console.log(`Salida de serverGPIO.py: ${data}`);
    });

    serverPyProcess.stderr.on('data', (data) => {
        console.error(`Error en serverGPIO.py: ${data}`);
    });

    serverPyProcess.on('close', (code) => {
        console.log(`Proceso serverGPIO.py terminó con código ${code}`);
    });
}

// Ejecutar las funciones periódicamente cada 40 segundos
function startPeriodicTasks() {
    if (qrCodeReader) {
        runScannerPy(); // Ejecutar la función una vez al iniciar
    }
    runServerPy(); // Ejecutar también la primera vez

    // Ejecutar cada 40 segundos
    setInterval(() => {
        if (qrCodeReader) {
            runScannerPy(); // Reiniciar el lector de QR
        }
        runServerPy(); // Reiniciar el servidor GPIO
    }, 40000); // 40,000 milisegundos = 40 segundos
}

const PORT = 7777;

app.listen(PORT, () => {
    console.log(`its alive on https://localhost:${PORT}`);
    startPeriodicTasks(); // Iniciar las tareas periódicas al iniciar el servidor
});

app.get('/', function (req, res) {
    res.sendFile(__dirname + '/index.html');
});
