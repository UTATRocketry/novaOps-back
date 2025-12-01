
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from data_file import users_db, pwd_context
from fastapi import WebSocketDisconnect

# Secret key for JWT encoding/decoding
SECRET_KEY = "your-secret-key"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30


# OAuth2PasswordBearer instance
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# Token creation function
def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# Password verification
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

# Fetch user from fake DB
def get_user(username: str):
    user = users_db.get(username)
    if user:
        user_dict = user.copy()
        return user_dict

# Authenticate user
def authenticate_user(username: str, password: str):
    user = get_user(username)
    if not user or not verify_password(password, user['password']):
        return False
    return user

# Get current user based on JWT token
async def get_current_user(token):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    # Validate the token
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise WebSocketDisconnect(code=1008)
    except JWTError:
        raise WebSocketDisconnect(code=1008)
    user = get_user(username)
    if user is None:
        raise credentials_exception
    return user