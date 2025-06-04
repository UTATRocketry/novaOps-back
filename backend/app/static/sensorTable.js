const wsUrl = "ws://192.168.0.1:8000/ws_basic";
let socket;
let reconnectAttempts = 0;
const maxReconnectAttempts = 10;
const reconnectInterval = 2000;

const statusElem = document.getElementById("status");
const reconnectStatusElem = document.getElementById("reconnect-status");
const sensorTableBody = document.getElementById("sensor-table-body");
const gpioTableBody = document.getElementById("gpio-table-body");

function updateSensorTable(sensors) {
  sensorTableBody.innerHTML = "";
  if (!Array.isArray(sensors) || sensors.length === 0) {
    sensorTableBody.innerHTML = "<tr><td colspan='2'>No sensor data available.</td></tr>";
    return;
  }

  sensors.forEach(sensor => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const valueCell = document.createElement("td");

    nameCell.textContent = sensor.name || "Unnamed";
    nameCell.style.width = '200px';
    nameCell.title = sensor.name;
    valueCell.textContent = sensor.value || "N/A";
    valueCell.style.width = '100px';
    valueCell.title = sensor.value;

    row.appendChild(nameCell);
    row.appendChild(valueCell);
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
      if (jsonData && jsonData.sensors) updateSensorTable(jsonData.sensors);
      else sensorTableBody.innerHTML = "<tr><td colspan='2'>Invalid data format.</td></tr>";
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
