const app = require("express")();
const gpio = require("rpi-gpio");
const gpiop = require("rpi-gpio").promise;
const {spawn} = require('child_process');
let {PythonShell} = require('python-shell')
const qrCodeReader = true;


function runPy(){
    return new Promise(async function(resolve, reject){
          let options = {
          mode: 'text',
          pythonOptions: ['-u'],
          scriptPath: './',//Path to your script
         };

          await PythonShell.run('python_code.py', options, function (err, results) {
          //On 'results' we get list of strings of all print done in your py scripts sequentially. 
          if (err) throw err;
          console.log('results: ');
          for(let i of results){
                console.log(i, "---->", typeof i)
          }
      resolve(results[1])//I returned only JSON(Stringified) out of all string I got from py script
     });
   })
 } 

function runMainQrCode(){
    return new Promise(async function(resolve, reject){
        let r =  await runPy()
        console.log(JSON.parse(JSON.stringify(r.toString())), "Done...!@")//Approach to parse string to JSON.
    })
 }

 if(qrCodeReader==true){
  runMainQrCode()
 }





const PORT = 8080;

app.listen(PORT, () => console.log(`its alive on https://localhost:${PORT}`));

let gates = [
  { id: 1, status: 0, pin: 7 },
  { id: 2, status: 0, pin: 11 },
  { id: 3, status: 0, pin: 12 },
  { id: 4, status: 0, pin: 13 },
  { id: 5, status: 0, pin: 15 },
  { id: 6, status: 0, pin: 16 },
  { id: 7, status: 0, pin: 18 },
  { id: 8, status: 0, pin: 22 },
];

app.get('/',function(req,res) {
  var dataToSend;
 // spawn new child process to call the python script
 const python = spawn('python', [__dirname + '/python_code.py']);
  res.sendFile(__dirname + '/index.html');
});


gates.forEach(gate => {
  gpiop
  .setup(gate.pin, gpiop.DIR_OUT)
  .then(() => {
    console.log(`Pin ${gate.pin} apagado.`);
    return gpiop.write(gate.pin, false);
  })
  .catch((err) => {
    console.log("Error: ", err.toString());
  });
});


app.get("/gateStatus", (req, res) => {
  console.log("gates");
  res.status(200).send(gates);
});

app.get("/gateStatus/:id", (req, res) => {
  const { id } = req.params;
  let gate = gates[id - 1];
  gpio.read(gate.pin, function (err, value) {
    if (err) throw err;
    res.status(200).send({ value: value });
  });
});

app.get("/openGate/:id", (req, res) => {
  const { id } = req.params;
  let gate = gates[id - 1];
  gate.status = 1;
  gpio.write(gate.pin, true, function (err) {
    if (err) throw err;
    res.status(200).send({ id: gate.id, status: gate.status });
  });
});

app.get("/closeGate/:id", (req, res) => {
  const { id } = req.params;
  let gate = gates[id - 1];
  gate.status = 0;
  gpio.write(gate.pin, false, function (err) {
    if (err) throw err;
    res.status(200).send({ id: gate.id, status: gate.status });
  });
});
