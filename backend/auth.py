from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
import base64
import hashlib
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Support both relative and absolute imports
try:
    from . import models, database
except ImportError:
    import models
    import database

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def _get_aes_key() -> bytes:
    key_b64 = os.getenv("PASSWORD_AES_KEY")
    if key_b64:
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            raise ValueError("PASSWORD_AES_KEY must decode to 32 bytes for AES-256")
        return key
    return hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()

def encrypt_password(plain_password: str) -> tuple[str, str]:
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)
    ciphertext = aesgcm.encrypt(nonce, plain_password.encode("utf-8"), None)
    return base64.b64encode(ciphertext).decode("utf-8"), base64.b64encode(nonce).decode("utf-8")

def verify_password(plain_password: str, password_ciphertext_b64: str, password_iv_b64: str) -> bool:
    try:
        key = _get_aes_key()
        aesgcm = AESGCM(key)
        nonce = base64.b64decode(password_iv_b64)
        ciphertext = base64.b64decode(password_ciphertext_b64)
        decrypted = aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
        return secrets.compare_digest(decrypted, plain_password)
    except Exception:
        return False

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(database.get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        user_id: int = payload.get("uid")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    query = db.query(models.User).filter(models.User.username == username)
    if user_id is not None:
        query = query.filter(models.User.id == user_id)
    user = query.first()
    if user is None:
        raise credentials_exception
    return user

async def get_current_manager(current_user: models.User = Depends(get_current_user)):
    if current_user.role != "manager":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user doesn't have enough privileges"
        )
    return current_user
