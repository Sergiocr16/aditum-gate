const app = require("express")();
const { exec } = require('child_process');
const configManager = require('./config-manager');

// Initialize configuration
configManager.initConfig();
const config = configManager.getConfig();

const qrCodeReader = config.device.scannerType === 'qr';
const hasScreen = config.hardware.hasScreen;
const hasTwoCameras = config.hardware.hasTwoCameras;

function runPy(script) {
    return new Promise((resolve, reject) => {
        exec(`sudo python3 ${script}`, { cwd: './' }, (error, stdout, stderr) => {
            if (error) {
                console.error(`exec error: ${error}`);
                reject(error);
                return;
            }
            if (stderr) {
                console.error(`stderr: ${stderr}`);
            }
            console.log(`stdout (${script}): ${stdout}`);
            resolve(stdout);
        });
    });
}

function runMainQrCode() {
    return new Promise(async function (resolve, reject) {
        try {
            const currentConfig = configManager.getConfig();
            const scannerScript = currentConfig.device.scannerScript;
            
            if (currentConfig.hardware.hasTwoCameras) {
                // Ejecutar ambos scripts en paralelo
                const [result1, result2] = await Promise.all([
                    runPy('scannerQr.py'),
                    runPy('scannerQrExit.py')
                ]);
                console.log("Resultados:", result1, result2);
            } else {
                // Ejecutar solo el script configurado si no hay dos cámaras
                await runPy(scannerScript);
            }
            resolve("Scripts ejecutados correctamente.");
        } catch (error) {
            reject(error);
        }
    });
}

async function runServerPy() {
    return new Promise((resolve, reject) => {
        exec('sudo python3 serverGPIO.py', { cwd: './' }, (error, stdout, stderr) => {
            if (error) {
                console.error(`exec error: ${error}`);
                reject(error);
                return;
            }
            if (stderr) {
                console.error(`stderr: ${stderr}`);
            }
            console.log(`stdout (serverGPIO.py): ${stdout}`);
            resolve(stdout);
        });
    });
}

const PORT = 7777;

app.listen(PORT, () => {
    console.log(`Server is running on https://localhost:${PORT}`);
    
    if (qrCodeReader) {
        runMainQrCode()
            .then((message) => console.log(message))
            .catch((error) => console.error("Error ejecutando scripts:", error));
    }

    runServerPy()
        .then((message) => console.log("Servidor GPIO ejecutado:", message))
        .catch((error) => console.error("Error ejecutando serverGPIO.py:", error));
});

app.get('/', function (req, res) {
    res.sendFile(__dirname + '/index.html');
});
