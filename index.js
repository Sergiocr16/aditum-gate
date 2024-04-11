const app = require("express")();
const {spawn} = require('child_process');
let {PythonShell} = require('python-shell')
const qrCodeReader = false;


function runPy(){
    return new Promise(async function(resolve, reject){
          let options = {
          mode: 'text',
          pythonOptions: ['-u'],
          scriptPath: './',//Path to your script
         };

          await PythonShell.run('scanner.py', options, function (err, results) {
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

function runServerPy(){
    return new Promise(async function(resolve, reject){
          let options = {
          mode: 'text',
          pythonOptions: ['-u'],
          scriptPath: './',//Path to your script
         };

          await PythonShell.run('serverGPIO.py', options, function (err, results) {
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

function runMainServerCode(){
    return new Promise(async function(resolve, reject){
        let r =  await runServerPy()
        console.log(JSON.parse(JSON.stringify(r.toString())), "Done...!@")//Approach to parse string to JSON.
    })
 }


const PORT = 7777;

app.listen(PORT, () => console.log(`its alive on https://localhost:${PORT}`));


app.get('/',function(req,res) {
  var dataToSend;
 // spawn new child process to call the python script
 const python = spawn('python', [__dirname + '/python_code.py']);
  res.sendFile(__dirname + '/index.html');
});


runServerPy()