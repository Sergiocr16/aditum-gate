const app = require("express")();
const { exec } = require('child_process');

const qrCodeReader = true;
const hasScreen = false;

// Simulación de variable para verificar si hay dos cámaras
const hasTwoCameras = true; // Cambia a `false` si solo hay una cámara

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
            if (hasTwoCameras) {
                   // Ejecutar ambos scripts en paralelo
              const [result1, result2] = await Promise.all([
                runPy('scannerQr.py'),
                runPy('scannerQrExit.py')
              ]);
            console.log("Resultados:", result1, result2);
            } else {
                // Ejecutar solo scanner.py si no hay dos cámaras
                await runPy('scannerQr.py');
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
