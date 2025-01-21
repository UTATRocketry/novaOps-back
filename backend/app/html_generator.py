# Dynamic HTML generation based on sensors and actuators
def generate_html(data_store):
    sensors_html = ""
    for sensor in data_store["sensors"]:
        sensors_html += f"""
        <pre><span id="{sensor['name']}_details">
    {{
        "name": "{sensor['name']}",
        "type": "{sensor['type']}",
        "value": <span id="{sensor['name']}_value">{sensor['value']}</span>
        "timestamp": <span id="{sensor['name']}_timestamp">{data_store["timestamp"]}</span>
    }}</span></pre>
        """

    actuators_html = ""
    for actuator in data_store["actuators"]:
        actuators_html += f"""
        <pre><span id="{actuator['name']}_details">
    {{
        "name": "{actuator['name']}",
        "type": "{actuator['type']}",
        "status": <span id="{actuator['name']}_status">{actuator['status']}</span>
        "timestamp": <span id="{actuator['name']}_timestamp">{data_store["timestamp"]}</span>
    }}</span></pre>
            <button id="{actuator['name']}_button" onclick="toggleActuator('{actuator['name']}')" style="background-color: lightgrey;">
                Toggle
            </button>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>Dashboard</title>
        </head>
        <body>
            <h1>Control Dashboard</h1>

            <!-- Login Form -->
            <div id="login-section">
                <h2>Login</h2>
                <form id="login-form" onsubmit="login(event)">
                    <label>Username: <input type="text" id="username" autocomplete="off" required/></label><br>
                    <label>Password: <input type="password" id="password" autocomplete="off" required/></label><br>
                    <button type="submit">Login</button>
                </form>
                <p id="login-error" style="color: red; display: none;">Incorrect login, please try again.</p>
            </div>

            <!-- Sensors and Actuators Section -->
            <div id="dashboard-section" style="display:none;">
                <h2>Sensors</h2>
                <ul id="sensors">
                    <!-- Sensor data will be inserted here -->
                    {sensors_html}
                </ul>

                <h2>Actuators</h2>
                <ul id="actuators">
                    <!-- Actuator data and buttons will be inserted here -->
                    {actuators_html}
                </ul>
            </div>

            <script>
                let ws;
                let token;

                function login(event) {{
                    event.preventDefault();
                    const username = document.getElementById('username').value;
                    const password = document.getElementById('password').value;

                    fetch('/token', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                        body: new URLSearchParams({{ 'username': username, 'password': password }})
                    }})
                    .then(response => {{
                        if (!response.ok) {{
                            throw new Error("Login failed");
                        }}
                        return response.json();
                    }})
                    .then(data => {{
                        token = data.access_token;
                        // Hide login and show dashboard
                        document.getElementById('login-section').style.display = 'none';
                        document.getElementById('dashboard-section').style.display = 'block';
                        connectWebSocket(token);
                    }})
                    .catch(error => {{
                        document.getElementById('login-error').style.display = 'block';
                    }});
                }}

                function connectWebSocket(token) {{
                    ws = new WebSocket(`ws://${{window.location.hostname}}:8000/ws?token=${{token}}`);

                    ws.onmessage = function(event) {{
                        const data = JSON.parse(event.data);
                        const sensors = data.sensors;
                        const actuators = data.actuators;
                        const timestamp = data.timestamp;

                        // Update sensor values
                        sensors.forEach(sensor => {{
                            const valueElement = document.getElementById(sensor.name + "_value");
                            const timestampElement = document.getElementById(sensor.name + "_timestamp");
                            if (valueElement) {{
                                valueElement.textContent = sensor.value;
                            }}
                            if (timestampElement) {{
                                timestampElement.textContent = data.timestamp;
                            }}
                        }});

                        // Update actuator status and button color
                        actuators.forEach(actuator => {{
                            const statusElement = document.getElementById(actuator.name + "_status");
                            const timestampElement = document.getElementById(actuator.name + "_timestamp");
                            const buttonElement = document.getElementById(actuator.name + "_button");

                            if (statusElement) {{
                                statusElement.textContent = actuator.status;
                            }}
                            if (timestampElement) {{
                                timestampElement.textContent = data.timestamp;
                            }}
                            if (buttonElement) {{
                                buttonElement.style.backgroundColor = actuator.status === "on" ? "darkgrey" : "lightgrey";
                            }}
                        }});
                    }};
                }}

                function toggleActuator(actuatorName) {{
                    ws.send(JSON.stringify({{"action": "toggle", "name": actuatorName}}));
                }}
            </script>
        </body>
    </html>
    """
    return html


new_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WebSocket JSON Viewer</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        pre { background: #f4f4f4; border: 1px solid #ddd; padding: 10px; overflow-x: auto; }
        #status { color: green; }
        #reconnect-status { color: orange; font-style: italic; }
    </style>
</head>
<body>
    <h1>WebSocket JSON Viewer</h1>
    <p>Status: <span id="status">Connecting...</span></p>
    <p id="reconnect-status"></p>
    <pre id="json-data">Waiting for data...</pre>
    <script>
        const wsUrl = "ws://raspberrypi.local:8000/ws_basic";
        let socket;
        let reconnectAttempts = 0;
        const maxReconnectAttempts = 10;
        const reconnectInterval = 2000; // 2 seconds

        const statusElem = document.getElementById("status");
        const dataElem = document.getElementById("json-data");
        const reconnectStatusElem = document.getElementById("reconnect-status");

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
                    const jsonData = JSON.parse(event.data);
                    dataElem.textContent = JSON.stringify(jsonData, null, 4);
                } catch (err) {
                    console.error("Error parsing JSON:", err);
                    dataElem.textContent = "Invalid JSON received.";
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
                reconnectAttempts++;
                reconnectStatusElem.textContent = `Reconnecting... (Attempt ${reconnectAttempts} of ${maxReconnectAttempts})`;
                setTimeout(() => {
                    connectWebSocket();
                }, reconnectInterval);
            } else {
                reconnectStatusElem.textContent = "Failed to reconnect after multiple attempts.";
                reconnectStatusElem.style.color = "red";
            }
        }

        // Start WebSocket connection
        connectWebSocket();
    </script>
</body>
</html>
"""