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

# AI Gate Edge Integration router (Requirement 1.1, 1.2, 1.7).
# The import is wrapped in a try/except so that a transient failure inside
# ``ai_gate`` (missing optional dep, syntax error during a deploy in progress,
# etc.) does NOT break the rest of the SAPA backend — startup must keep
# going even when the AI Gate module fails to load (Requirement 1.7).
# Sentinels default to ``None`` so the ``include_router`` and ``mount``
# call sites further down can guard against the missing module.
try:
    from .ai_gate import router as ai_gate_router, static_faces_router
except Exception as exc:  # pragma: no cover - defensive: keep backend booting
    print(
        f"[ai_gate] failed to load module=ai_gate exc={type(exc).__name__} msg={exc}",
        flush=True,
    )
    ai_gate_router = None
    static_faces_router = None

# Create tables
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="SAPA IoT Dashboard API")

# Wire AI Gate routers when the module loaded successfully. The custom
# ``static_faces_router`` MUST be registered before the fallback
# ``StaticFiles`` mount below so the regex-validated 400/405 responses
# take precedence over the generic StaticFiles handler (FastAPI/Starlette
# routing is order-sensitive).
if ai_gate_router is not None:
    app.include_router(ai_gate_router)
if static_faces_router is not None:
    app.include_router(static_faces_router)


@app.on_event("startup")
async def _ai_gate_startup() -> None:
    """Initialise AI Gate side-effects (MQTT publisher, etc.).

    Failures here are logged but never raised — Requirement 1.7 mandates
    that the rest of the backend keep booting even when AI Gate cannot.
    """

    if ai_gate_router is None:
        return
    try:
        from . import ai_gate as _ai_gate_mod
        await _ai_gate_mod.startup()
    except Exception as exc:
        print(
            f"[ai_gate] startup failed: {type(exc).__name__}: {exc}",
            flush=True,
        )


@app.on_event("shutdown")
async def _ai_gate_shutdown() -> None:
    """Release AI Gate side-effects on FastAPI shutdown."""

    if ai_gate_router is None:
        return
    try:
        from . import ai_gate as _ai_gate_mod
        await _ai_gate_mod.shutdown()
    except Exception as exc:
        print(
            f"[ai_gate] shutdown failed: {type(exc).__name__}: {exc}",
            flush=True,
        )


@app.get("/")
def root():
    return {"service": "sapa", "ok": True}


@app.get("/health")
def health():
    return {"ok": True}

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
_gate_lock = Lock()
_gate_status = {
    "status": "closed",
    "last_action": "none",
    "timestamp": datetime.now(timezone.utc).isoformat(),
}

# IoT (ESP32) presence tracking via MQTT heartbeat / LWT
_iot_lock = Lock()
_iot_state = {
    "last_seen": None,  # datetime | None
    "online_payload": None,  # last status payload from device
}
_IOT_OFFLINE_AFTER_SECONDS = int(os.getenv("IOT_OFFLINE_AFTER_SECONDS", "15"))


def _mark_iot_seen(payload: dict | None = None):
    with _iot_lock:
        _iot_state["last_seen"] = datetime.now(timezone.utc)
        if payload is not None:
            _iot_state["online_payload"] = payload


def _get_iot_state():
    with _iot_lock:
        ts = _iot_state["last_seen"]
        last_payload = _iot_state["online_payload"]
    if ts is None:
        return {"connected": False, "last_seen": None, "age_ms": None, "payload": None}
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    online = age < _IOT_OFFLINE_AFTER_SECONDS
    return {
        "connected": bool(online),
        "last_seen": ts,
        "age_ms": int(age * 1000),
        "payload": last_payload,
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

# AI Gate fallback static mount (Requirements 1.4, 7.1).
# The custom ``static_faces_router`` registered above performs regex
# validation on the filename and returns 400 for malformed names. This
# mount serves as a fallback for methods/paths the router does not
# match (e.g. HEAD vs GET behaviour handled by StaticFiles, 404 for
# missing files), and is intentionally registered AFTER the router so
# the router takes precedence (FastAPI/Starlette dispatch is in
# registration order). ``check_dir=False`` prevents an ImportError when
# the faces directory has not been created yet.
app.mount(
    "/api/static/faces",
    StaticFiles(
        directory=os.path.join(_uploads_root, "faces"),
        check_dir=False,
    ),
    name="ai_gate_faces",
)


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

# Add CORS middleware.
# Origins are taken from CORS_ALLOW_ORIGINS (comma-separated). When unset OR
# set to "*", we fall back to a permissive configuration WITHOUT credentials
# (browsers reject `*` + credentials anyway). For production, set:
#   CORS_ALLOW_ORIGINS=https://sapa.farhn.dev,https://www.sapa.farhn.dev
_cors_origins_raw = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
if _cors_origins_raw == "*" or not _cors_origins_raw:
    _cors_origins: list[str] = ["*"]
    _cors_allow_credentials = False
else:
    _cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    _cors_allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MQTT Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
MQTT_TOPIC_GATE = os.getenv("MQTT_TOPIC_GATE", "sapa/gate")
MQTT_TOPIC_ATTENDANCE = os.getenv("MQTT_TOPIC_ATTENDANCE", "sapa/attendance")
MQTT_TOPIC_DEVICE_STATUS = os.getenv("MQTT_TOPIC_DEVICE_STATUS", "sapa/device/status")
MQTT_TOPIC_DEVICE_HEARTBEAT = os.getenv("MQTT_TOPIC_DEVICE_HEARTBEAT", "sapa/device/heartbeat")
MQTT_TOPIC_PIR = os.getenv("MQTT_TOPIC_PIR", "sapa/pir")

mqtt_client = mqtt.Client()
if MQTT_USERNAME:
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD or "")

def on_connect(client, _userdata, _flags, rc):
    print(f"Connected to MQTT Broker with result code {rc}")
    client.subscribe(MQTT_TOPIC_ATTENDANCE)
    client.subscribe(MQTT_TOPIC_DEVICE_STATUS)
    client.subscribe(MQTT_TOPIC_DEVICE_HEARTBEAT)
    client.subscribe(MQTT_TOPIC_PIR)

def _publish_gate_command(action: str, employee_id: str | None = None, reason: str | None = None):
    payload = {
        "action": action,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if employee_id:
        payload["employee_id"] = employee_id
    if reason:
        payload["reason"] = reason
    try:
        mqtt_client.publish(MQTT_TOPIC_GATE, json.dumps(payload))
    except Exception as e:
        print(f"MQTT publish failed: {e}")

def on_message(_client, _userdata, msg):
    topic = msg.topic
    raw = msg.payload.decode(errors="ignore")
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}

    if topic == MQTT_TOPIC_DEVICE_STATUS or topic == MQTT_TOPIC_DEVICE_HEARTBEAT:
        _mark_iot_seen(payload if isinstance(payload, dict) else {"raw": raw})
        return

    if topic == MQTT_TOPIC_PIR:
        # PIR sensor reports motion after pass-through; close the gate.
        _mark_iot_seen(payload if isinstance(payload, dict) else None)
        with _gate_lock:
            _gate_status["status"] = "closed"
            _gate_status["last_action"] = "auto_close_pir"
            _gate_status["timestamp"] = datetime.now(timezone.utc).isoformat()
        _publish_gate_command("close", reason="pir_motion")
        return

    if topic != MQTT_TOPIC_ATTENDANCE:
        return

    # treat any inbound from device as heartbeat
    _mark_iot_seen(payload if isinstance(payload, dict) else None)

    employee_id = payload.get("employee_id") if isinstance(payload, dict) else None
    is_valid = payload.get("is_valid") if isinstance(payload, dict) else None
    direction = (payload.get("direction") if isinstance(payload, dict) else None) or "in"
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
                        "source": "edge_mqtt",
                    }
                )
            except Exception:
                _publish_gate_command("invalid", reason="db_unavailable")
                return
            with _gate_lock:
                _gate_status["status"] = "open"
                _gate_status["last_action"] = "auto_open_face_match"
                _gate_status["timestamp"] = datetime.now(timezone.utc).isoformat()
            with _edge_lock:
                _edge_last_event["ts"] = timestamp
                _edge_last_event["is_valid"] = True
                _edge_last_event["employee_id"] = employee.id
                _edge_last_event["message"] = f"Welcome {employee.name}"
            _publish_gate_command("open", employee_id=employee.id)
        else:
            with _edge_lock:
                _edge_last_event["ts"] = timestamp
                _edge_last_event["is_valid"] = False
                _edge_last_event["employee_id"] = employee_id
                _edge_last_event["message"] = "Wajah tidak dikenali"
            _publish_gate_command("invalid", reason="unknown_face")
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
    ts = datetime.now(timezone.utc)
    if (
        not user
        or not user.password_ciphertext
        or not user.password_iv
        or not auth.verify_password(payload.password, user.password_ciphertext, user.password_iv)
    ):
        # Record failed login attempt to MongoDB audit_logs (Username, Status, Timestamp)
        try:
            mongo_db.get_audit_collection().insert_one(
                {
                    "timestamp": ts,
                    "username": payload.username,
                    "user_id": payload.user_id,
                    "role": user.role if user else None,
                    "event_type": "login_failed",
                    "status": "failed",
                    "message": "Incorrect credentials",
                }
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
        )
    access_token = auth.create_access_token(data={"sub": user.username, "role": user.role, "uid": user.id})
    # Record successful login (Username, Status, Timestamp)
    try:
        mongo_db.get_audit_collection().insert_one(
            {
                "timestamp": ts,
                "username": user.username,
                "user_id": user.id,
                "role": user.role,
                "event_type": "login_success",
                "status": "success",
                "message": "Login success",
            }
        )
    except Exception:
        pass
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


@app.get("/employees/{employee_id}/faces")
def get_employee_faces(employee_id: str, db: Session = Depends(get_db)):
    db_employee = db.query(models.Employee).filter(models.Employee.id == employee_id).first()
    if not db_employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    faces_dir = os.path.join(os.path.dirname(__file__), "uploads", "faces")
    faces = []
    
    # Check for face files with common extensions
    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
        face_path = os.path.join(faces_dir, f"{employee_id}{ext}")
        if os.path.exists(face_path):
            faces.append({
                "url": f"/uploads/faces/{employee_id}{ext}",
                "filename": f"{employee_id}{ext}",
            })
    
    return {"employee_id": employee_id, "faces": faces}

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


@app.post("/roles/", response_model=schemas.Role)
def create_role(
    payload: schemas.RoleCreate,
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_manager),
):
    division = payload.division.strip()
    position = payload.position.strip()
    if not division or not position:
        raise HTTPException(status_code=400, detail="Division and Position are required")
    exists = (
        db.query(models.Role)
        .filter(models.Role.division == division, models.Role.position == position)
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="Role already exists")
    role = models.Role(
        division=division,
        position=position,
        description=(payload.description.strip() if payload.description else None),
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


@app.delete("/roles/{role_id}")
def delete_role(
    role_id: int,
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_manager),
):
    role = db.query(models.Role).filter(models.Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    db.delete(role)
    db.commit()
    return {"detail": "Role deleted"}


@app.get("/roles/", response_model=List[schemas.Role])
def list_roles(
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user),
):
    return db.query(models.Role).order_by(models.Role.division.asc(), models.Role.position.asc()).all()


@app.get("/roles/divisions")
def list_divisions(
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user),
):
    rows = db.query(models.Role.division).distinct().all()
    divisions = sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()})
    return {"divisions": divisions}


@app.get("/roles/positions")
def list_positions(
    division: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user),
):
    q = db.query(models.Role)
    if division:
        q = q.filter(models.Role.division == division)
    items = q.order_by(models.Role.position.asc()).all()
    return {"positions": [{"id": r.id, "division": r.division, "position": r.position} for r in items]}


@app.get("/roles/stats", response_model=List[schemas.RoleStats])
def role_stats(
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_manager),
):
    """For each role: total employees, how many are 'active today' (present at least once today), and inactive."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    active_ids: set[str] = set()
    try:
        cursor = mongo_db.get_logs_collection().find(
            {
                "timestamp": {"$gte": today_start, "$lt": today_end},
                "status": "present",
            },
            {"employee_id": 1},
        )
        for doc in cursor:
            eid = doc.get("employee_id")
            if eid:
                active_ids.add(str(eid))
    except Exception:
        # MongoDB unavailable -> just report totals without active counts.
        active_ids = set()

    out: list[dict] = []
    roles = db.query(models.Role).order_by(models.Role.division.asc(), models.Role.position.asc()).all()
    for r in roles:
        members = (
            db.query(models.Employee)
            .filter(models.Employee.division == r.division, models.Employee.position == r.position)
            .all()
        )
        total = len(members)
        active = sum(1 for emp in members if emp.id in active_ids)
        out.append({
            "id": r.id,
            "division": r.division,
            "position": r.position,
            "description": r.description,
            "total": total,
            "active_today": active,
            "inactive_today": max(0, total - active),
        })
    return out

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
    stale = age_ms > 3000
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
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    # Accept either a valid EDGE_INGEST_KEY (used by the headless edge server)
    # OR a valid logged-in JWT (used when /edge is opened in a browser by an
    # authenticated user). If neither is provided and EDGE_INGEST_KEY is set,
    # the request is rejected.
    required = os.getenv("EDGE_INGEST_KEY", "")
    has_valid_key = bool(required) and (x_edge_key or "") == required
    has_valid_token = False
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            await auth.get_current_user(token=token, db=db)
            has_valid_token = True
        except HTTPException:
            has_valid_token = False
        except Exception:
            has_valid_token = False

    if required and not (has_valid_key or has_valid_token):
        raise HTTPException(status_code=401, detail="Invalid edge key or token")
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


@app.get("/gate/status")
def get_gate_status():
    with _gate_lock:
        gs = _gate_status.copy()
    iot = _get_iot_state()
    return {
        "status": gs.get("status", "closed"),
        "last_action": gs.get("last_action", "none"),
        "timestamp": gs.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "iot_connected": iot["connected"],
        "iot_last_seen": iot["last_seen"],
        "iot_age_ms": iot["age_ms"],
    }


@app.get("/iot/status")
def get_iot_status():
    iot = _get_iot_state()
    return {
        "connected": iot["connected"],
        "last_seen": iot["last_seen"],
        "age_ms": iot["age_ms"],
        "payload": iot["payload"],
        "offline_after_seconds": _IOT_OFFLINE_AFTER_SECONDS,
    }


@app.post("/gate/control")
def control_gate(request: dict, current_user: models.User = Depends(auth.get_current_user)):
    action = request.get("action")
    gate_id = request.get("gate_id", "default")

    if action not in ["open", "close"]:
        raise HTTPException(status_code=400, detail="Invalid action. Must be 'open' or 'close'")

    iot = _get_iot_state()
    if not iot["connected"]:
        raise HTTPException(
            status_code=503,
            detail="Gate device offline (no MQTT heartbeat). Tidak bisa kirim perintah.",
        )

    with _gate_lock:
        _gate_status["status"] = "open" if action == "open" else "closed"
        _gate_status["last_action"] = f"manual_{action}_by_{current_user.username}"
        _gate_status["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        mqtt_client.publish(
            MQTT_TOPIC_GATE,
            json.dumps({
                "action": action,
                "gate_id": gate_id,
                "source": "manual",
                "by": current_user.username,
                "ts": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception:
        pass

    try:
        mongo_db.get_audit_collection().insert_one(
            {
                "timestamp": datetime.now(timezone.utc),
                "username": current_user.username,
                "user_id": current_user.id,
                "role": current_user.role,
                "event_type": f"gate_{action}",
                "status": "success",
                "message": f"Manual gate {action} via dashboard",
            }
        )
    except Exception:
        pass

    return {"ok": True, "action": action, "iot_connected": True}


@app.post("/edge/face-match")
def report_face_match(payload: schemas.FaceMatchEvent):
    """Receive face recognition result from edge server (AI runs on edge)."""
    if payload.edge_key:
        required = os.getenv("EDGE_INGEST_KEY", "")
        if required and payload.edge_key != required:
            raise HTTPException(status_code=401, detail="Invalid edge key")

    timestamp = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        employee = None
        if payload.employee_id:
            employee = db.query(models.Employee).filter(models.Employee.id == str(payload.employee_id)).first()

        if payload.is_valid and employee:
            try:
                mongo_db.get_logs_collection().insert_one(
                    {
                        "employee_id": employee.id,
                        "timestamp": timestamp,
                        "direction": payload.direction or "in",
                        "status": "present",
                        "reason": None,
                        "source": "edge_ai",
                        "confidence": payload.confidence,
                    }
                )
            except Exception:
                _publish_gate_command("invalid", reason="db_unavailable")
                raise HTTPException(status_code=503, detail="MongoDB is not available")

            with _gate_lock:
                _gate_status["status"] = "open"
                _gate_status["last_action"] = "auto_open_face_match"
                _gate_status["timestamp"] = timestamp.isoformat()
            with _edge_lock:
                _edge_last_event["ts"] = timestamp
                _edge_last_event["is_valid"] = True
                _edge_last_event["employee_id"] = employee.id
                _edge_last_event["message"] = f"Welcome {employee.name}"
            _publish_gate_command("open", employee_id=employee.id)
            return {"ok": True, "action": "open", "employee_id": employee.id}
        else:
            with _edge_lock:
                _edge_last_event["ts"] = timestamp
                _edge_last_event["is_valid"] = False
                _edge_last_event["employee_id"] = payload.employee_id
                _edge_last_event["message"] = payload.message or "Wajah tidak dikenali"
            _publish_gate_command("invalid", reason=payload.message or "unknown_face")
            return {"ok": True, "action": "invalid"}
    finally:
        db.close()


@app.get("/audit/logins")
def list_login_audit(
    _current_user: models.User = Depends(auth.get_current_manager),
    limit: int = Query(200, ge=1, le=2000),
):
    """Login activity (Username, Status, Timestamp) stored on the VPS via MongoDB."""
    try:
        cursor = (
            mongo_db.get_audit_collection()
            .find({"event_type": {"$in": ["login_success", "login_failed"]}})
            .sort("timestamp", -1)
            .limit(limit)
        )
        docs = list(cursor)
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is not available")
    out = []
    for d in docs:
        out.append({
            "username": d.get("username"),
            "status": d.get("status") or ("success" if d.get("event_type") == "login_success" else "failed"),
            "timestamp": d.get("timestamp"),
            "role": d.get("role"),
            "user_id": d.get("user_id"),
            "message": d.get("message"),
        })
    return out
