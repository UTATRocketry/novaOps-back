# Initial data
from passlib.context import CryptContext

# Password hashing context
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# User Data
users_db = {
    "user1": {"username": "user1", "password":  pwd_context.hash("pass1"), "role": "operator"},
    "user2": {"username": "user2", "password":  pwd_context.hash("pass2"), "role": "viewer"},
}

# Store for sensors and actuators
data_store = {
    "timestamp":"",
    "sensors": [
        {"name": "TGSO-G", "type": "Thermocouple", "value": 0},
        {"name": "TOT", "type": "Thermocouple", "value": 0},
        {"name": "TFM", "type": "Thermocouple", "value": 0},
        {"name": "PGSO", "type": "Pressure Transducer", "value": 0},
        {"name": "PGS", "type": "Pressure Transducer", "value": 0},
        {"name": "POTT", "type": "Pressure Transducer", "value": 0},
        {"name": "POTB", "type": "Pressure Transducer", "value": 0},
        {"name": "PFT", "type": "Pressure Transducer", "value": 0},
        {"name": "PFM", "type": "Pressure Transducer", "value": 0},
        {"name": "PCC", "type": "Pressure Transducer", "value": 0},
        {"name": "MOT", "type": "Load Cell", "value": 0},
        {"name": "MFT", "type": "Load Cell", "value": 0}
    ],
    "actuators": [
        {"name": "BVGSO", "type": "Servo Actuator", "status": "off"},
        {"name": "BVGSP", "type": "Servo Actuator", "status": "off"},
        {"name": "BVOTP", "type": "Servo Actuator", "status": "off"},
        {"name": "BVFTP", "type": "Servo Actuator", "status": "off"},
        {"name": "MFV", "type": "Servo Actuator", "status": "off"},
        {"name": "SVGSD", "type": "Solenoid Valve", "status": "off"},
        {"name": "SVBVGS", "type": "Solenoid Valve", "status": "off"},
        {"name": "SVMOVP", "type": "Solenoid Valve", "status": "off"},
        {"name": "SVOTD", "type": "Solenoid Valve", "status": "off"},
        {"name": "SVOTV", "type": "Solenoid Valve", "status": "off"},
        {"name": "SVFTV", "type": "Solenoid Valve", "status": "off"},
    ]
}
