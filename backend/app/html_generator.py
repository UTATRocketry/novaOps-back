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
                    ws = new WebSocket("ws://localhost:8000/ws?token=" + token);

                    ws.onmessage = function(event) {{
                        const data = JSON.parse(event.data);
                        const sensors = data.sensors;
                        const actuators = data.actuators;

                        // Update sensor values
                        sensors.forEach(sensor => {{
                            const element = document.getElementById(sensor.name + "_value");
                            if (element) {{
                                element.textContent = sensor.value;
                            }}
                        }});

                        // Update actuator status and button color
                        actuators.forEach(actuator => {{
                            const statusElement = document.getElementById(actuator.name + "_status");
                            const buttonElement = document.getElementById(actuator.name + "_button");

                            if (statusElement) {{
                                statusElement.textContent = actuator.status;
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
