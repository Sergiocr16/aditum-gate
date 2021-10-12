const app = require("express")();
const gpio = require("rpi-gpio");
const gpiop = require("rpi-gpio").promise;

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


gates.forEach(gate => {
  gpiop
  .setup(gate.pin, gpiop.DIR_LOW)
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
