const app = require("express")();
const { spawn, exec } = require('child_process');
const { PythonShell } = require('python-shell');

const qrCodeReader = true;
const hasScreen = false;

function runPy() {
    return new Promise((resolve, reject) => {
        exec('sudo python3 scanner.py', { cwd: './' }, (error, stdout, stderr) => {
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

function runMainQrCode() {
    return new Promise(async function (resolve, reject) {
        let r = await runPy()
        console.log(JSON.parse(JSON.stringify(r.toString())), "Done...!@")
    })
}

async function runServerPy() {
    let r = await new Promise(function (resolve, reject) {
        let options = {
            mode: 'text',
            pythonOptions: ['-u'],
            scriptPath: './', // Path to your script
        };

        PythonShell.run('serverGPIO.py', options, function (err, results) {
            if (err) throw err;
            console.log('results: ');
            for (let i of results) {
                console.log(i, "---->", typeof i)
            }
            resolve(results[1])
        });
    });
    console.log(JSON.parse(JSON.stringify(r.toString())), "Done...!@");
}

const PORT = 7777;

app.listen(PORT, () => {
    console.log(`its alive on https://localhost:${PORT}`);
    
    if (qrCodeReader) {
        runMainQrCode();
    }

    runServerPy();
});

app.get('/', function (req, res) {
    var dataToSend;
    // spawn new child process to call the python script
    const python = spawn('python', [__dirname + '/python_code.py']);
    res.sendFile(__dirname + '/index.html');
});
