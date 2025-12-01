const wsUrl = "ws://192.168.0.1:8000/ws_basic";
let socket;
let reconnectAttempts = 0;
const maxReconnectAttempts = 10;
const reconnectInterval = 2000;

const statusElem = document.getElementById("status");
const delayElem = document.getElementById("delay");
const reconnectStatusElem = document.getElementById("reconnect-status");
const sensorTableBody = document.getElementById("sensor-table-body");
const gpioTableBody = document.getElementById("gpio-table-body");

function updateSensorTable(sensors) {
  sensorTableBody.innerHTML = "";
  if (!Array.isArray(sensors) || sensors.length === 0) {
    sensorTableBody.innerHTML = "<tr><td colspan='4'>No sensor data available.</td></tr>";
    return;
  }
  sensors.forEach(sensor => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const valueCell = document.createElement("td");
    const avgCell = document.createElement("td");
    const rateCell = document.createElement("td");

    nameCell.textContent = sensor.name || "Unnamed";
    nameCell.style.width = '200px';
    nameCell.title = sensor.name;
    valueCell.textContent = sensor.value; // || "N/A";
    valueCell.style.width = '100px';
    valueCell.title = sensor.value;
    avgCell.textContent = sensor.avg || "N/A";
    rateCell.textContent = sensor.rate || "N/A";
 
    row.appendChild(nameCell);
    row.appendChild(valueCell);
    row.appendChild(avgCell);
    row.appendChild(rateCell);
    sensorTableBody.appendChild(row);
  });
}

function updateGpioTable(gpios) {
  gpioTableBody.innerHTML = "";
  if (!Array.isArray(gpios) || gpios.length === 0) {
    gpioTableBody.innerHTML = "<tr><td colspan='2'>No GPIO data available.</td></tr>";
    return;
  }
  gpios.forEach(gpio => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const stateCell = document.createElement("td");
    nameCell.textContent = gpio.name || "Unnamed";
    nameCell.style.width = '200px';
    nameCell.title = gpio.name;
    stateCell.textContent = gpio.state; // || "N/A";
    stateCell.style.width = '100px';
    stateCell.title = gpio.state;
    row.appendChild(nameCell);
    row.appendChild(stateCell);
    gpioTableBody.appendChild(row);
  });
}
function connectWebSocket() {
  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    statusElem.textContent = "Connected";
    statusElem.style.color = "green";
    reconnectStatusElem.textContent = "";
    reconnectAttempts = 0;
  };

  socket.onmessage = event => {
    try {
      const jsonData = JSON.parse(event.data);
      if (jsonData && jsonData.sensors) {
        let receivedTime = Date.now() * 0.001//performance.timeOrigin + performance.now();
        let sampleTime = jsonData.sensors[0].timestamp * 0.001
        let delay = receivedTime - sampleTime
        delayElem.textContent = `${delay.toFixed(2)} s`;//`${receivedTime}-${jsonData.sensors[0].timestamp}`// `${delay.toFixed(2)} ms`;
        updateSensorTable(jsonData.sensors);
      }
      else sensorTableBody.innerHTML = "<tr><td colspan='4'>Invalid data format.</td></tr>";
      if (jsonData && jsonData.gpios) updateGpioTable(jsonData.gpios);
      else gpioTableBody.innerHTML = "<tr><td colspan='2'>Invalid GPIO data format.</td></tr>";
    } catch (err) {
      console.error("Error parsing JSON:", err);
      sensorTableBody.innerHTML = "<tr><td colspan='2'>Invalid JSON received.</td></tr>";
    }
  };

  socket.onerror = error => {
    statusElem.textContent = "Error";
    statusElem.style.color = "red";
    console.error("WebSocket error:", error);
  };

  socket.onclose = () => {
    statusElem.textContent = "Disconnected";
    statusElem.style.color = "orange";
    attemptReconnect();
  };
}

function attemptReconnect() {
  if (reconnectAttempts < maxReconnectAttempts) {
    reconnectAttempts++;
    reconnectStatusElem.textContent = `Reconnecting... (${reconnectAttempts})`;
    setTimeout(connectWebSocket, reconnectInterval);
  } else {
    reconnectStatusElem.textContent = "Failed to reconnect.";
    reconnectStatusElem.style.color = "red";
  }
}

//window.addEventListener("load", () => {
//  connectWebSocket();
//});


// window.onload = 
connectWebSocket();
