from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Query, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import List, Optional
import paho.mqtt.client as mqtt
import imghdr
import json
import os
from datetime import datetime, timedelta, timezone
from threading import Lock
import random

from . import models, schemas, auth, mongo_db, system_metrics
from .database import engine, get_db, SessionLocal

# Create tables
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="SAPA IoT Dashboard API")

_edge_lock = Lock()
_edge_last_event = {
    "ts": datetime.now(timezone.utc),
    "is_valid": None,
    "employee_id": None,
    "message": None,
}
_edge_last_frame = {
    "ts": None,
    "content_type": None,
    "bytes": None,
}


def _ensure_user_profile_columns():
    dialect = engine.dialect.name
    if dialect == "sqlite":
        with engine.begin() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
            if "full_name" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR"))
            if "avatar_filename" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN avatar_filename VARCHAR"))
            if "permissions_json" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN permissions_json TEXT"))
        return
    if dialect == "postgresql":
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'users'"
                )
            ).fetchall()
            cols = {r[0] for r in rows}
            if "full_name" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR"))
            if "avatar_filename" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN avatar_filename VARCHAR"))
            if "permissions_json" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN permissions_json TEXT"))


_ensure_user_profile_columns()

_uploads_root = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(_uploads_root, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_root), name="uploads")


def _user_to_dict(user: models.User):
    avatar_url = f"/uploads/avatars/{user.avatar_filename}" if getattr(user, "avatar_filename", None) else None
    perms: list[str] = []
    raw = getattr(user, "permissions_json", None)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                perms = [str(x) for x in parsed if isinstance(x, (str, int, float, bool))]
        except Exception:
            perms = []
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": getattr(user, "full_name", None),
        "avatar_url": avatar_url,
        "role": user.role,
        "permissions": perms,
    }

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MQTT Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC_GATE = "sapa/gate"
MQTT_TOPIC_ATTENDANCE = "sapa/attendance"

mqtt_client = mqtt.Client()

def on_connect(client, _userdata, _flags, rc):
    print(f"Connected to MQTT Broker with result code {rc}")
    client.subscribe(MQTT_TOPIC_ATTENDANCE)

def on_message(_client, _userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        mqtt_client.publish(MQTT_TOPIC_GATE, json.dumps({"action": "invalid"}))
        return

    employee_id = payload.get("employee_id")
    is_valid = payload.get("is_valid")
    direction = payload.get("direction", "in")
    timestamp = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        employee = None
        if employee_id:
            employee = db.query(models.Employee).filter(models.Employee.id == str(employee_id)).first()

        if is_valid and employee:
            try:
                mongo_db.get_logs_collection().insert_one(
                    {
                        "employee_id": employee.id,
                        "timestamp": timestamp,
                        "direction": direction,
                        "status": "present",
                        "reason": None,
                    }
                )
            except Exception:
                mqtt_client.publish(MQTT_TOPIC_GATE, json.dumps({"action": "invalid"}))
                return
            mqtt_client.publish(MQTT_TOPIC_GATE, json.dumps({"action": "open", "employee_id": employee.id}))
        else:
            mqtt_client.publish(MQTT_TOPIC_GATE, json.dumps({"action": "invalid"}))
    finally:
        db.close()

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()
except Exception as e:
    print(f"Could not connect to MQTT Broker: {e}")

@app.post("/login", response_model=schemas.Token)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    query = db.query(models.User).filter(models.User.username == payload.username)
    if payload.user_id is not None:
        query = query.filter(models.User.id == payload.user_id)
    user = query.first()
    if (
        not user
        or not user.password_ciphertext
        or not user.password_iv
        or not auth.verify_password(payload.password, user.password_ciphertext, user.password_iv)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
        )
    access_token = auth.create_access_token(data={"sub": user.username, "role": user.role, "uid": user.id})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user.role,
        "user_id": user.id,
        "permissions": _user_to_dict(user).get("permissions", []),
    }

# Employee Management (Manager Only)
@app.post("/employees/", response_model=schemas.Employee)
def create_employee(employee: schemas.EmployeeCreate, db: Session = Depends(get_db), _current_user: models.User = Depends(auth.get_current_manager)):
    employee_id = None
    for _ in range(20):
        candidate = str(random.randint(100000, 999999))
        exists = db.query(models.Employee).filter(models.Employee.id == candidate).first()
        if not exists:
            employee_id = candidate
            break
    if not employee_id:
        raise HTTPException(status_code=500, detail="Could not generate employee ID")

    db_employee = models.Employee(id=employee_id, **employee.model_dump())
    db.add(db_employee)
    db.commit()
    db.refresh(db_employee)
    return db_employee

@app.post("/employees/{employee_id}/face")
def upload_employee_face(
    employee_id: str,
    face_image: UploadFile = File(...),
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_manager),
):
    db_employee = db.query(models.Employee).filter(models.Employee.id == employee_id).first()
    if not db_employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    ext = os.path.splitext(face_image.filename or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        ext = ".jpg"

    faces_dir = os.path.join(os.path.dirname(__file__), "uploads", "faces")
    os.makedirs(faces_dir, exist_ok=True)
    target_path = os.path.join(faces_dir, f"{employee_id}{ext}")
    with open(target_path, "wb") as f:
        f.write(face_image.file.read())
    return {"detail": "Face saved", "employee_id": employee_id}

@app.delete("/employees/{employee_id}")
def delete_employee(employee_id: str, db: Session = Depends(get_db), _current_user: models.User = Depends(auth.get_current_manager)):
    db_employee = db.query(models.Employee).filter(models.Employee.id == employee_id).first()
    if not db_employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    db.delete(db_employee)
    db.commit()
    return {"detail": "Employee deleted"}

@app.get("/employees/", response_model=List[schemas.Employee])
def list_employees(db: Session = Depends(get_db), _current_user: models.User = Depends(auth.get_current_user)):
    return db.query(models.Employee).all()

@app.get("/users/me", response_model=schemas.User)
def get_me(current_user: models.User = Depends(auth.get_current_user)):
    return _user_to_dict(current_user)


@app.get("/users/", response_model=List[schemas.User])
def list_users(_current_user: models.User = Depends(auth.get_current_manager), db: Session = Depends(get_db)):
    users = db.query(models.User).order_by(models.User.id.asc()).all()
    return [_user_to_dict(u) for u in users]


@app.post("/users/", response_model=schemas.User)
def create_user(
    payload: schemas.ManagedUserCreate,
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_manager),
):
    if not payload.username or len(payload.username.strip()) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if not payload.password or len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    username = payload.username.strip()
    exists = db.query(models.User).filter(models.User.username == username).first()
    if exists:
        raise HTTPException(status_code=409, detail="Username already exists")

    email = (payload.email.strip() if payload.email else None)
    if email:
        exists = db.query(models.User).filter(models.User.email == email).first()
        if exists:
            raise HTTPException(status_code=409, detail="Email already exists")

    user_id = None
    for _ in range(30):
        candidate = random.randint(100000, 999999)
        exists = db.query(models.User).filter(models.User.id == candidate).first()
        if not exists:
            user_id = candidate
            break
    if not user_id:
        raise HTTPException(status_code=500, detail="Could not generate user ID")

    cipher, iv = auth.encrypt_password(payload.password)
    perms = payload.permissions or []
    perms = [str(p) for p in perms if isinstance(p, str) and p.strip()]
    db_user = models.User(
        id=user_id,
        username=username,
        email=email,
        full_name=payload.full_name,
        password_ciphertext=cipher,
        password_iv=iv,
        role=payload.role,
        permissions_json=json.dumps(perms),
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return _user_to_dict(db_user)


@app.patch("/users/me", response_model=schemas.ProfileUpdateResponse)
def update_me(
    payload: schemas.UserUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    id_changed = False
    username_changed = False

    if payload.id is not None and payload.id != current_user.id:
        exists = db.query(models.User).filter(models.User.id == payload.id).first()
        if exists:
            raise HTTPException(status_code=409, detail="User ID already exists")
        current_user.id = payload.id
        id_changed = True

    if payload.username is not None and payload.username != current_user.username:
        exists = db.query(models.User).filter(models.User.username == payload.username).first()
        if exists:
            raise HTTPException(status_code=409, detail="Username already exists")
        current_user.username = payload.username
        username_changed = True

    if payload.email is not None and payload.email != current_user.email:
        if payload.email:
            exists = (
                db.query(models.User)
                .filter(models.User.email == payload.email, models.User.id != current_user.id)
                .first()
            )
            if exists:
                raise HTTPException(status_code=409, detail="Email already exists")
        current_user.email = payload.email

    if payload.full_name is not None:
        current_user.full_name = payload.full_name

    db.commit()
    db.refresh(current_user)

    token = None
    if id_changed or username_changed:
        token = auth.create_access_token(
            data={"sub": current_user.username, "role": current_user.role, "uid": current_user.id}
        )

    return {"user": _user_to_dict(current_user), "access_token": token, "token_type": "bearer" if token else None}


@app.post("/users/me/avatar", response_model=schemas.ProfileUpdateResponse)
async def upload_my_avatar(
    avatar: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    if avatar.content_type not in ("image/png", "image/jpeg"):
        raise HTTPException(status_code=400, detail="Only PNG/JPEG is allowed")

    data = await avatar.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large")

    kind = imghdr.what(None, h=data)
    if kind not in ("png", "jpeg"):
        raise HTTPException(status_code=400, detail="Only PNG/JPEG is allowed")

    ext = ".png" if kind == "png" else ".jpg"
    avatars_dir = os.path.join(_uploads_root, "avatars")
    os.makedirs(avatars_dir, exist_ok=True)

    if getattr(current_user, "avatar_filename", None) and current_user.avatar_filename.endswith((".png", ".jpg", ".jpeg")):
        old_path = os.path.join(avatars_dir, current_user.avatar_filename)
        try:
            if os.path.exists(old_path):
                os.remove(old_path)
        except Exception:
            pass

    filename = f"user_{current_user.id}{ext}"
    target_path = os.path.join(avatars_dir, filename)
    with open(target_path, "wb") as f:
        f.write(data)

    current_user.avatar_filename = filename
    db.commit()
    db.refresh(current_user)
    return {"user": _user_to_dict(current_user), "access_token": None, "token_type": None}


@app.post("/users/me/change-password")
def change_my_password(
    payload: schemas.ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    if not auth.verify_password(payload.old_password, current_user.password_ciphertext, current_user.password_iv):
        raise HTTPException(status_code=400, detail="Old password is incorrect")
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    cipher, iv = auth.encrypt_password(payload.new_password)
    current_user.password_ciphertext = cipher
    current_user.password_iv = iv
    db.commit()
    return {"detail": "ok"}


@app.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    current_user: models.User = Depends(auth.get_current_manager),
    db: Session = Depends(get_db),
):
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete the current user")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "manager":
        raise HTTPException(status_code=400, detail="Manager users cannot be deleted")
    if user.role != "admin":
        raise HTTPException(status_code=400, detail="Only admin users can be deleted")
    db.delete(user)
    db.commit()
    return {"detail": "User deleted"}


@app.post("/audit")
def create_audit_event(
    payload: schemas.AuditEventCreate,
    current_user: models.User = Depends(auth.get_current_user),
):
    ts = datetime.now(timezone.utc)
    try:
        mongo_db.get_audit_collection().insert_one(
            {
                "timestamp": ts,
                "user_id": current_user.id,
                "username": current_user.username,
                "role": current_user.role,
                "event_type": payload.event_type,
                "message": payload.message,
            }
        )
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is not available")
    return {"detail": "ok"}


@app.get("/system/metrics")
def get_system_usage(_current_user: models.User = Depends(auth.get_current_manager)):
    return system_metrics.get_system_metrics()

# Attendance
@app.post("/attendance/", response_model=schemas.Attendance)
def manual_attendance(attendance: schemas.AttendanceCreate, db: Session = Depends(get_db), _current_user: models.User = Depends(auth.get_current_user)):
    db_employee = db.query(models.Employee).filter(models.Employee.id == attendance.employee_id).first()
    if not db_employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = datetime.now(timezone.utc)
    try:
        mongo_db.get_logs_collection().insert_one(
            {
                "employee_id": attendance.employee_id,
                "timestamp": ts,
                "direction": attendance.direction,
                "status": attendance.status,
                "reason": attendance.reason,
            }
        )
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is not available")
    return {
        "employee_id": attendance.employee_id,
        "timestamp": ts,
        "direction": attendance.direction,
        "status": attendance.status,
        "reason": attendance.reason,
        "employee": db_employee,
    }

@app.get("/attendance/", response_model=List[schemas.Attendance])
def list_attendance(
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user),
    date: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    day: Optional[int] = Query(None),
    employee_id: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(200, ge=1, le=2000),
):
    query: dict = {}
    if employee_id:
        query["employee_id"] = employee_id
    if direction:
        query["direction"] = direction
    if status_filter:
        query["status"] = status_filter

    range_start = None
    if date:
        try:
            range_start = datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            range_start = None
    elif year and month and day:
        try:
            range_start = datetime(year=year, month=month, day=day)
        except Exception:
            range_start = None

    if range_start:
        range_start = range_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if range_start.tzinfo is None:
            range_start = range_start.replace(tzinfo=timezone.utc)
        range_end = range_start + timedelta(days=1)
        query["timestamp"] = {"$gte": range_start, "$lt": range_end}

    try:
        cursor = mongo_db.get_logs_collection().find(query).sort("timestamp", -1).limit(limit)
        docs = list(cursor)
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is not available")
    employee_ids = list({d.get("employee_id") for d in docs if d.get("employee_id")})
    employees = db.query(models.Employee).filter(models.Employee.id.in_(employee_ids)).all() if employee_ids else []
    employee_by_id = {e.id: e for e in employees}

    result = []
    for d in docs:
        eid = d.get("employee_id")
        ts = d.get("timestamp") or datetime.now(timezone.utc)
        result.append(
            {
                "employee_id": str(eid) if eid is not None else "",
                "timestamp": ts,
                "direction": d.get("direction") or "in",
                "status": d.get("status") or "present",
                "reason": d.get("reason"),
                "employee": employee_by_id.get(str(eid)) if eid is not None else None,
            }
        )
    return [r for r in result if r["employee"] is not None]

# Real-time monitoring placeholder
@app.get("/monitoring/stream")
def get_stream_url():
    return {"url": os.getenv("EDGE_STREAM_URL", "")}


def _edge_status_payload(now: datetime):
    with _edge_lock:
        last = dict(_edge_last_event)
    ts = last.get("ts") or now
    age_ms = int(max(0, (now - ts).total_seconds() * 1000))
    stale = age_ms > 5000
    return {
        "ts": ts,
        "is_valid": None if stale else last.get("is_valid"),
        "employee_id": None if stale else last.get("employee_id"),
        "message": (None if stale else last.get("message")) or ("Menunggu..." if stale else None),
        "stale": stale,
        "age_ms": age_ms,
    }


@app.get("/edge/status", response_model=schemas.EdgeStatus)
def get_edge_status():
    now = datetime.now(timezone.utc)
    return _edge_status_payload(now)


@app.post("/edge/events", response_model=schemas.EdgeStatus)
def push_edge_event(payload: schemas.EdgeEvent):
    now = datetime.now(timezone.utc)
    with _edge_lock:
        _edge_last_event["ts"] = now
        _edge_last_event["is_valid"] = bool(payload.is_valid)
        _edge_last_event["employee_id"] = payload.employee_id
        _edge_last_event["message"] = payload.message
    return _edge_status_payload(now)


@app.post("/edge/frame")
async def push_edge_frame(
    frame: UploadFile = File(...),
    x_edge_key: Optional[str] = Header(default=None, alias="X-EDGE-KEY"),
):
    required = os.getenv("EDGE_INGEST_KEY", "")
    if required and (x_edge_key or "") != required:
        raise HTTPException(status_code=401, detail="Invalid edge key")
    raw = await frame.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty frame")
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Frame too large")
    detected = imghdr.what(None, raw)
    if detected != "jpeg":
        raise HTTPException(status_code=415, detail="Only JPEG is allowed")
    now = datetime.now(timezone.utc)
    with _edge_lock:
        _edge_last_frame["ts"] = now
        _edge_last_frame["content_type"] = "image/jpeg"
        _edge_last_frame["bytes"] = raw
    return {"ok": True, "ts": now}


@app.get("/edge/frame.jpg")
def get_edge_frame(_t: Optional[int] = Query(default=None, alias="t")):
    now = datetime.now(timezone.utc)
    with _edge_lock:
        ts = _edge_last_frame.get("ts")
        ct = _edge_last_frame.get("content_type")
        data = _edge_last_frame.get("bytes")
    if not ts or not data or not ct:
        raise HTTPException(status_code=404, detail="No frame")
    age_ms = int(max(0, (now - ts).total_seconds() * 1000))
    if age_ms > 5000:
        raise HTTPException(status_code=404, detail="Frame stale")
    return Response(
        content=data,
        media_type=ct,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
