(function () {
  "use strict";

const WS_URL = window.APP_CONFIG?.WS_URL || `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
const wsUrl = `${WS_URL}/ws?role=operator`;

let socket;
let reconnectAttempts = 0;
const maxReconnectAttempts = 10;
const reconnectInterval = 2000;

const statusElem = document.getElementById("status");
const reconnectStatusElem = document.getElementById("reconnect-status");
const sensorTableBody = document.getElementById("sensor-table-body");
const gpioTableBody = document.getElementById("gpio-table-body");

gpioTableBody.innerHTML = "<tr><td colspan='2'>GPIO stream is not provided by this backend.</td></tr>";

function updateSensorTable(sensors) {
  sensorTableBody.innerHTML = "";
  if (!Array.isArray(sensors) || sensors.length === 0) {
    sensorTableBody.innerHTML = "<tr><td colspan='4'>No sensor data available.</td></tr>";
    return;
  }

  sensors.forEach((sensor) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const valueCell = document.createElement("td");
    const avgCell = document.createElement("td");

    nameCell.textContent = sensor.name || "Unnamed";
    valueCell.textContent = sensor.value ?? "N/A";
    avgCell.textContent = sensor.value ?? "N/A";

    row.appendChild(nameCell);
    row.appendChild(valueCell);
    row.appendChild(avgCell);
    sensorTableBody.appendChild(row);
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

  socket.onmessage = (event) => {
    try {
      const message = JSON.parse(event.data);
      
      if (message.type === "session") {
        window.dispatchEvent(new CustomEvent("nova:session", { detail: message }));
      }

      if (message.type === "parsed_data" && Array.isArray(message.sensors)) {
        updateSensorTable(message.sensors);
      }

      if (message.type === "actuator_states") {
        window.dispatchEvent(new CustomEvent("nova:actuator_states", { detail: message.actuator_states || {} }));
      }

      if (message.type === "snapshot") {
        window.dispatchEvent(new CustomEvent("nova:actuator_states", { detail: message.actuator_states || {} }));
      }
    } catch (err) {
      console.error("Error parsing websocket message:", err);
    }
  };

  socket.onerror = (error) => {
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
    reconnectAttempts += 1;
    reconnectStatusElem.textContent = `Reconnecting... (${reconnectAttempts})`;
    setTimeout(connectWebSocket, reconnectInterval);
  } else {
    reconnectStatusElem.textContent = "Failed to reconnect.";
    reconnectStatusElem.style.color = "red";
  }
}

connectWebSocket();

})();
