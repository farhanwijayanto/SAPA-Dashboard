"""AI Gate Edge Integration router (foundation module).

This module exposes a FastAPI ``router`` with prefix ``/api/edge`` that bridges
the SAPA Dashboard, the Edge laptop, and the ESP32 gate. It is intentionally
self-contained and is **not** allowed to import from ``backend.main``,
``backend.models``, ``backend.schemas``, ``backend.database``, or
``backend.mongo_db`` (Requirement 1.5).

This file is the foundation layer (Task 2.1):

* declares the ``router`` object
* declares the Pydantic request/response schemas
* declares safe environment-variable helpers (``_safe_port`` / ``_safe_topic``)
* declares async ``startup`` / ``shutdown`` lifecycle stubs (filled in by
  later tasks)

The module MUST be import-side-effect free: importing it MUST NOT open a
network socket, modify the database schema, or touch the filesystem beyond
reading environment variables (Requirement 1.3 / Property 19). All side
effects (MQTT connect, MongoDB client construction, filesystem listing) are
deferred to request handlers and the ``startup`` coroutine that later tasks
will populate.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import secrets
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import paho.mqtt.client as paho_mqtt_client
import pymongo
from fastapi import (
    APIRouter,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from pymongo.errors import PyMongoError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# Pillow is required for the PNG → JPEG transcode path of ``FaceStorage``
# (Requirement 2.2). We wrap the import in a try/except so that a transient
# packaging issue (e.g. a partially-installed virtualenv) does not turn the
# module-level import into a hard failure — that would block every other
# AI Gate endpoint from loading. The sentinel ``_pillow_available`` is then
# consulted by ``FaceStorage.write_jpeg`` which raises
# ``RuntimeError("pillow_unavailable")`` only when the PNG path is actually
# requested. JPEG uploads do not require Pillow.
try:
    from PIL import Image  # type: ignore[import-not-found]

    _pillow_available = True
except Exception:  # pragma: no cover - defensive: missing Pillow
    Image = None  # type: ignore[assignment]
    _pillow_available = False

# ---------------------------------------------------------------------------
# Environment variable keys (Requirements 6.1, 5.5, 1.5)
#
# These are *names* of the env vars consulted by helpers and request
# handlers. We read them via the helpers below so that the parsing /
# fallback policy lives in one place.
# ---------------------------------------------------------------------------

ENV_MQTT_BROKER = "MQTT_BROKER"
ENV_MQTT_PORT = "MQTT_PORT"
ENV_MQTT_USERNAME = "MQTT_USERNAME"
ENV_MQTT_PASSWORD = "MQTT_PASSWORD"
ENV_MQTT_TOPIC_GATE = "MQTT_TOPIC_GATE"
ENV_MONGODB_URI = "MONGODB_URI"
ENV_MONGODB_DB = "MONGODB_DB"
ENV_EDGE_INGEST_KEY = "EDGE_INGEST_KEY"
ENV_SECRET_KEY = "SECRET_KEY"
ENV_DATABASE_URL = "DATABASE_URL"

# Defaults referenced by helpers and later tasks.
DEFAULT_MQTT_BROKER = "sapa-mosquitto"
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_USERNAME = "backend"
DEFAULT_MQTT_PASSWORD = ""
DEFAULT_MQTT_TOPIC_GATE = "sapa/gate"
DEFAULT_MONGODB_URI = "mongodb://localhost:27017"
DEFAULT_MONGODB_DB = "sapa"

# MQTT topic syntax allowed by the broker plus a sane length cap
# (Requirement 6.4).
_TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_/+#-]+$")
_TOPIC_MAX_LEN = 128

# Valid MQTT TCP port range (Requirement 6.3 narrows to 1024..65535 to avoid
# privileged-port collisions).
_MQTT_PORT_MIN = 1024
_MQTT_PORT_MAX = 65535


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _safe_port(env_var: str, default: int) -> int:
    """Return the integer port configured at ``env_var`` or ``default``.

    The value is read from ``os.environ``. Falls back to ``default`` (and
    logs to stdout) when the env var is missing, blank, not parsable as an
    integer, or outside the inclusive range ``[1024, 65535]``
    (Requirement 6.3 / Property 14).
    """

    raw = os.environ.get(env_var)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(
            f"[ai_gate] env {env_var}={raw!r} is not an integer, "
            f"falling back to {default}",
            flush=True,
        )
        return default
    if value < _MQTT_PORT_MIN or value > _MQTT_PORT_MAX:
        print(
            f"[ai_gate] env {env_var}={value} outside "
            f"[{_MQTT_PORT_MIN},{_MQTT_PORT_MAX}], falling back to {default}",
            flush=True,
        )
        return default
    return value


def _safe_topic(env_var: str, default: str) -> str:
    """Return the MQTT topic configured at ``env_var`` or ``default``.

    The value is read from ``os.environ``. Falls back to ``default`` (and
    logs to stdout) when the env var is missing, blank, longer than 128
    characters, or contains any character outside ``[A-Za-z0-9_/+#-]``
    (Requirement 6.4 / Property 14).
    """

    raw = os.environ.get(env_var)
    if raw is None or raw == "":
        return default
    if len(raw) > _TOPIC_MAX_LEN or not _TOPIC_PATTERN.match(raw):
        print(
            f"[ai_gate] env {env_var}={raw!r} is not a valid MQTT topic, "
            f"falling back to {default!r}",
            flush=True,
        )
        return default
    return raw


# ---------------------------------------------------------------------------
# Pydantic schemas (data contracts)
# ---------------------------------------------------------------------------


class FaceMatchRequest(BaseModel):
    """Request body for ``POST /api/edge/face-match`` (Requirement 4)."""

    # Reject unexpected fields (Requirement 4.5 / property: extra="forbid").
    model_config = ConfigDict(extra="forbid")

    # ``StrictBool`` enforces Requirement 4.5 verbatim: only the JSON
    # literals ``true`` / ``false`` are accepted. Pydantic's default
    # ``bool`` would coerce strings like ``"yes"`` or integers like
    # ``1`` into ``True`` and let semantically invalid bodies through
    # to step 3 of the face-match handler — that would surface as a
    # 400 rather than the mandated 422.
    is_valid: StrictBool
    employee_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    direction: Optional[Literal["in", "out"]] = "in"
    message: Optional[str] = Field(default=None, max_length=200)


class FaceMatchResponse(BaseModel):
    """Response body for ``POST /api/edge/face-match`` (Requirement 4.3)."""

    action: Literal["open", "invalid"]
    employee_id: Optional[str] = None
    logged: bool


class FaceListItem(BaseModel):
    """Single entry in the faces listing (Requirement 3.2)."""

    employee_id: str
    url: str


class FacesListResponse(BaseModel):
    """Response body for ``GET /api/edge/faces`` (Requirement 3.1)."""

    faces: List[FaceListItem]


class UploadResponse(BaseModel):
    """Response body for ``POST /api/edge/upload-face/{employee_id}``
    (Requirement 2.1)."""

    employee_id: str
    saved_path: str


# ---------------------------------------------------------------------------
# Internal helpers (Task 3.1)
#
# The three classes below are pure validation / verification helpers used by
# the request handlers implemented in later tasks (4.1, 5.1, 7.1). Keeping
# them as small, single-responsibility units lets us property-test the
# decision logic without spinning up a TestClient.
# ---------------------------------------------------------------------------


class ImageValidator:
    """Whitelist content-type + magic-byte + size validator for face uploads.

    Validates uploads against Requirement 2.4 (content-type whitelist),
    Requirement 2.5 (magic-byte must match declared content-type),
    Requirement 2.6 (size cap 5 MB), and Requirement 2.10 (employee_id
    regex ``^[A-Za-z0-9_-]{1,64}$``).
    """

    ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png"})
    MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5 MiB exactly (5,242,880 bytes).
    JPEG_MAGIC = b"\xff\xd8\xff"
    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    EMPLOYEE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    @classmethod
    def validate_employee_id(cls, employee_id: Any) -> bool:
        """Return ``True`` if ``employee_id`` matches the allowed regex.

        Returns ``False`` for non-string inputs, the empty string, strings
        longer than 64 characters, or strings containing characters
        outside ``[A-Za-z0-9_-]``.
        """

        if not isinstance(employee_id, str):
            return False
        return cls.EMPLOYEE_ID_PATTERN.match(employee_id) is not None

    @classmethod
    def validate_content_type(cls, content_type: Any) -> Optional[str]:
        """Return the canonical kind (``"jpeg"`` or ``"png"``) or ``None``.

        The match is case-insensitive on the type/subtype but ignores any
        trailing parameters (e.g. ``image/jpeg; charset=binary``). Returns
        ``None`` when ``content_type`` is missing or not in the whitelist.
        """

        if not isinstance(content_type, str):
            return None
        # Strip trailing parameters such as "; charset=binary" and trim.
        primary = content_type.split(";", 1)[0].strip().lower()
        if primary == "image/jpeg":
            return "jpeg"
        if primary == "image/png":
            return "png"
        return None

    @classmethod
    def validate_magic_bytes(cls, blob: Any, expected_kind: str) -> bool:
        """Return ``True`` when ``blob``'s prefix matches ``expected_kind``.

        ``expected_kind`` MUST be one of ``"jpeg"`` or ``"png"`` (the values
        returned by :meth:`validate_content_type`). Any other value yields
        ``False``. ``blob`` may be ``bytes`` or ``bytearray``; other types
        yield ``False``.
        """

        if not isinstance(blob, (bytes, bytearray)):
            return False
        if expected_kind == "jpeg":
            return bytes(blob[: len(cls.JPEG_MAGIC)]) == cls.JPEG_MAGIC
        if expected_kind == "png":
            return bytes(blob[: len(cls.PNG_MAGIC)]) == cls.PNG_MAGIC
        return False

    @classmethod
    def validate_size(cls, size: Any) -> bool:
        """Return ``True`` when ``size`` is a non-negative int ≤ 5 MiB."""

        if isinstance(size, bool) or not isinstance(size, int):
            return False
        return 0 <= size <= cls.MAX_SIZE_BYTES


class JwtVerifier:
    """Decode JWT bearer tokens and extract ``sub`` + ``role`` claims.

    Reads ``SECRET_KEY`` from the environment **at verify time** (not at
    import or instantiation) so tests can monkeypatch the env var per
    request without rebuilding the verifier (Requirement 1.3 /
    Property 19). Uses HS256 to stay compatible with ``backend.auth``
    even though we deliberately do not import that module
    (Requirement 1.5).

    Raises :class:`fastapi.HTTPException`:

    * **401** ``invalid_token`` — for missing/malformed/expired tokens or
      bad signature (Requirement 2.7).
    * **403** ``forbidden_role`` — when the decoded ``role`` claim is not
      in ``{"manager", "admin"}`` (Requirement 2.8).
    """

    ALGORITHM = "HS256"
    ALLOWED_ROLES = frozenset({"manager", "admin"})

    def verify(self, authorization_header: Optional[str]) -> Dict[str, Any]:
        """Validate ``authorization_header`` and return the JWT claims.

        ``authorization_header`` is the raw ``Authorization`` header value
        (e.g. ``"Bearer eyJ..."``). Returns the decoded claims dict on
        success. Raises ``HTTPException(401, "invalid_token")`` on any
        decoding failure and ``HTTPException(403, "forbidden_role")`` when
        the role claim is not in the allowlist.
        """

        token = self._extract_bearer_token(authorization_header)
        secret = os.environ.get(ENV_SECRET_KEY) or ""
        # Empty SECRET_KEY would let jose decode tokens signed with an
        # empty key, which is dangerous — treat it as misconfigured and
        # reject the token (Requirement 2.7).
        if not secret:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
            )
        try:
            claims = jwt.decode(token, secret, algorithms=[self.ALGORITHM])
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
            )
        if not isinstance(claims, dict):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
            )
        sub = claims.get("sub")
        role = claims.get("role")
        # `sub` is required for downstream auditing; bail out early when
        # absent to avoid leaking a partial identity.
        if not isinstance(sub, str) or not sub:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
            )
        if not isinstance(role, str) or role not in self.ALLOWED_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="forbidden_role",
            )
        return claims

    @staticmethod
    def _extract_bearer_token(authorization_header: Optional[str]) -> str:
        """Return the bearer token portion or raise 401 ``invalid_token``."""

        if not authorization_header or not isinstance(authorization_header, str):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
            )
        # RFC 6750: scheme is case-insensitive; require single space
        # separator and a non-empty token component.
        parts = authorization_header.strip().split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
            )
        token = parts[1].strip()
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_token",
            )
        return token


class EdgeKeyVerifier:
    """Constant-time check of ``X-EDGE-KEY`` against ``EDGE_INGEST_KEY`` env.

    Reads ``EDGE_INGEST_KEY`` from the environment on every call so tests
    can flip the variable without recreating the verifier (and so that
    the import remains side-effect-free per Requirement 1.3).

    Returns ``True`` when the header value matches the configured key
    using :func:`secrets.compare_digest` (Requirement 4.11). Returns
    ``False`` when either the header or the env var is missing/empty —
    callers translate the latter into HTTP 503
    ``edge_auth_misconfigured`` in Task 5.1.
    """

    def check(self, header_value: Optional[str]) -> bool:
        """Return ``True`` only when both header and env are non-empty
        and match in constant time."""

        configured = os.environ.get(ENV_EDGE_INGEST_KEY, "") or ""
        if not configured:
            return False
        if not isinstance(header_value, str) or not header_value:
            return False
        # Use bytes form so compare_digest never short-circuits on
        # length differences in a way observable via timing.
        return secrets.compare_digest(
            header_value.encode("utf-8"),
            configured.encode("utf-8"),
        )


# ---------------------------------------------------------------------------
# Face storage (Task 3.2)
#
# ``FaceStorage`` encapsulates all filesystem access for the faces directory:
# atomic writes (Requirements 2.3, 2.13), PNG → JPEG transcode
# (Requirement 2.2), directory creation on write (Requirement 2.11), and
# case-insensitive listing with deterministic ordering (Requirements 3.1,
# 3.2, 3.5).
# ---------------------------------------------------------------------------


# Default location of ``Faces_Storage`` resolved relative to this module
# (``backend/ai_gate.py`` → ``backend/uploads/faces``). Computed at the
# class level so tests can override either via the constructor argument
# or via the ``FACES_STORAGE_DIR`` env var without monkeypatching anything
# at import time.
_DEFAULT_FACES_DIR = Path(__file__).resolve().parent / "uploads" / "faces"
ENV_FACES_STORAGE_DIR = "FACES_STORAGE_DIR"

# Allowed face image extensions for listing — lowercase canonical form
# (case-insensitive matching is implemented in ``list_faces``). Per
# Requirement 3.1 the listing accepts ``.jpg``, ``.jpeg``, and ``.png``.
_FACE_LIST_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})


class FaceStorage:
    """Atomic, thread-safe face image writer + lister.

    The storage directory is resolved at write/list time (not at
    construction) so that tests and operators can flip the
    ``FACES_STORAGE_DIR`` env var or pass an alternate path through the
    constructor without rebuilding the singleton. Writes use the
    well-known ``write-temp + os.replace`` dance to guarantee that no
    half-written bytes are ever visible at the canonical
    ``{employee_id}.jpg`` path (Requirement 2.3) and that no orphaned
    ``.tmp`` file is left behind on failure (Requirement 2.13).
    """

    JPEG_QUALITY = 90

    def __init__(self, faces_dir: Optional[Union[str, Path]] = None) -> None:
        # Store the raw override; resolution happens lazily so changes to
        # ``FACES_STORAGE_DIR`` between construction and the first write
        # are still honoured.
        self._faces_dir_override: Optional[Path]
        if faces_dir is None:
            self._faces_dir_override = None
        else:
            self._faces_dir_override = Path(faces_dir)

    @property
    def faces_dir(self) -> Path:
        """Return the absolute path to ``Faces_Storage``.

        Order of precedence: explicit constructor argument → env var
        ``FACES_STORAGE_DIR`` → repo default ``backend/uploads/faces``.
        The result is always returned as an absolute path (resolved
        without requiring the directory to exist).
        """

        if self._faces_dir_override is not None:
            target = self._faces_dir_override
        else:
            env_value = os.environ.get(ENV_FACES_STORAGE_DIR, "") or ""
            if env_value:
                target = Path(env_value)
            else:
                target = _DEFAULT_FACES_DIR
        # ``Path.resolve(strict=False)`` returns an absolute path without
        # requiring the directory to exist yet — perfect for first-write.
        return target.resolve(strict=False) if not target.is_absolute() else target

    def write_jpeg(
        self,
        employee_id: str,
        image_bytes: bytes,
        source_kind: Literal["jpeg", "png"],
    ) -> Path:
        """Persist ``image_bytes`` as ``{employee_id}.jpg`` atomically.

        ``source_kind`` MUST be the canonical kind returned by
        :meth:`ImageValidator.validate_content_type` (``"jpeg"`` or
        ``"png"``). For PNG inputs the bytes are decoded via Pillow,
        converted to RGB, and re-encoded as JPEG with quality 90
        (Requirement 2.2). For JPEG inputs the bytes are written
        verbatim. Either way the final file at
        ``{faces_dir}/{employee_id}.jpg`` is produced via
        ``os.replace`` for atomicity (Requirement 2.3). On any error
        the ``.tmp`` artefact is removed (Requirement 2.13) and the
        exception is re-raised so the request handler can map it to
        HTTP 500 ``storage_write_failed``.
        """

        target_dir = self.faces_dir
        # Requirement 2.11: ensure parent directory exists *before* we
        # touch any file. ``exist_ok=True`` is idempotent and avoids a
        # TOCTOU race with concurrent uploads.
        target_dir.mkdir(parents=True, exist_ok=True)

        final_path = target_dir / f"{employee_id}.jpg"
        # Tmp filename includes pid+tid so concurrent writers in the
        # same process never collide on the same scratch path
        # (Property: writes are isolated per pid+tid).
        tmp_name = (
            f"{employee_id}.jpg.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        tmp_path = target_dir / tmp_name

        try:
            if source_kind == "png":
                if not _pillow_available or Image is None:
                    raise RuntimeError("pillow_unavailable")
                # Pillow's JPEG encoder cannot serialise images with an
                # alpha channel, so normalise to RGB first. The decode
                # also doubles as a structural validity check — a
                # malformed PNG raises here before we touch the disk.
                with Image.open(io.BytesIO(image_bytes)) as im:
                    rgb = im.convert("RGB")
                    with open(tmp_path, "wb") as fh:
                        rgb.save(fh, format="JPEG", quality=self.JPEG_QUALITY)
            elif source_kind == "jpeg":
                # JPEG bytes are written as-is; the upload handler is
                # responsible for verifying the magic bytes prior to
                # this call (Requirement 2.5).
                with open(tmp_path, "wb") as fh:
                    fh.write(image_bytes)
            else:  # pragma: no cover - defensive against unexpected kinds
                raise ValueError(f"unsupported source_kind={source_kind!r}")

            # ``os.replace`` is atomic on the same filesystem on both
            # POSIX and Windows — this is the linchpin of Requirement
            # 2.3 (no half-written bytes at the canonical path).
            os.replace(tmp_path, final_path)
            return final_path
        except Exception:
            # Best-effort cleanup so a failed write never leaves a
            # ``.tmp`` orphan in ``Faces_Storage`` (Requirement 2.13).
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                # Swallow cleanup errors — we still want to surface the
                # original write failure to the caller.
                pass
            raise

    def list_faces(self) -> List[Tuple[str, str]]:
        """Return ``(employee_id, filename)`` tuples for all face files.

        The directory is *not* created on listing (Requirement 3.5: a
        missing directory yields the empty list). Filenames are filtered
        by case-insensitive extension against ``.jpg`` / ``.jpeg`` /
        ``.png`` (Requirement 3.1) and the result is sorted
        lexicographically by stem (the ``employee_id`` portion) to
        guarantee a deterministic ordering for callers (Requirement
        3.5).
        """

        target_dir = self.faces_dir
        if not target_dir.exists() or not target_dir.is_dir():
            return []

        entries: List[Tuple[str, str]] = []
        # ``iterdir`` raises ``OSError`` on permission failures; the
        # request handler in Task 5.1 maps that to HTTP 500
        # ``faces_storage_unreadable``. We deliberately do NOT swallow
        # it here so the caller can distinguish "empty dir" from
        # "unreadable dir".
        for entry in target_dir.iterdir():
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext not in _FACE_LIST_EXTENSIONS:
                continue
            entries.append((entry.stem, entry.name))

        entries.sort(key=lambda pair: pair[0])
        return entries


# ---------------------------------------------------------------------------
# Employee lookup (Task 3.2)
#
# Thin SQLAlchemy core helper that issues a single parametrised SELECT
# against the ``employees`` table. Used by the face-match handler
# (Task 7.1) to confirm an ``employee_id`` exists before logging an
# attendance record. The lookup is deliberately best-effort — when the
# database is unreachable (or no ``DATABASE_URL`` is configured at all)
# the helper returns ``None`` so the caller can degrade gracefully
# rather than 500-ing on transient DB issues (Requirement 5.5: scope is
# limited to ``employees`` reads + ``attendance_logs`` / ``audit_logs``
# Mongo writes).
# ---------------------------------------------------------------------------


class EmployeeLookup:
    """Lazy SQLAlchemy core lookup for ``employees.id``.

    The engine is created on first :meth:`exists` call (Requirement 1.3:
    importing ``ai_gate`` MUST NOT open a network socket) and cached
    thereafter for the lifetime of the process. ``DATABASE_URL`` is
    re-read each call so the helper transparently picks up env var
    changes between requests in tests.
    """

    _LOOKUP_SQL = text("SELECT id FROM employees WHERE id = :id LIMIT 1")

    def __init__(self) -> None:
        self._engine: Any = None
        self._engine_url: str = ""
        # Guard concurrent first-call engine creation so we never build
        # two engines for the same URL.
        self._engine_lock = threading.Lock()

    def _get_engine(self, database_url: str) -> Any:
        """Return a cached engine, building it lazily on first call.

        Rebuilds the engine if ``DATABASE_URL`` changes between calls
        (rare in production, useful in tests).
        """

        with self._engine_lock:
            if self._engine is None or self._engine_url != database_url:
                # ``pool_pre_ping`` keeps stale connections from
                # surviving across DB restarts in long-lived backends.
                self._engine = create_engine(
                    database_url,
                    pool_pre_ping=True,
                )
                self._engine_url = database_url
            return self._engine

    def exists(self, employee_id: str) -> Optional[bool]:
        """Return ``True`` / ``False`` / ``None`` for the lookup outcome.

        * ``True`` — a row was found in ``employees`` matching ``id``.
        * ``False`` — the query ran successfully and returned no rows.
        * ``None`` — best-effort failure; either ``DATABASE_URL`` is
          empty (no DB configured) or SQLAlchemy raised. The caller
          treats ``None`` as "unknown" and proceeds without blocking
          the gate decision.
        """

        database_url = os.environ.get(ENV_DATABASE_URL, "") or ""
        if not database_url:
            # No DB configured — caller treats this as "unknown".
            return None

        try:
            engine = self._get_engine(database_url)
            with engine.connect() as conn:
                row = conn.execute(
                    self._LOOKUP_SQL,
                    {"id": employee_id},
                ).first()
            return row is not None
        except SQLAlchemyError as exc:
            # Best-effort: log the failure but do not propagate. This
            # matches the design's "AI Gate keeps gating decisions
            # available even if Postgres is briefly unreachable".
            print(
                f"[ai_gate] EmployeeLookup failed for "
                f"employee_id={employee_id!r}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return None


# ---------------------------------------------------------------------------
# MQTT publisher (Task 3.3)
#
# ``MqttGatePublisher`` is the single proc-wide bridge between the AI Gate
# request handlers and the Mosquitto broker. The class is deliberately
# **lazy**: ``__init__`` only stores configuration (no socket, no thread)
# so importing ``ai_gate`` cannot block on DNS or open a port (Requirement
# 1.3 / Property 19). The actual ``paho.mqtt.client.Client`` is built in
# :meth:`startup`, which is invoked by the FastAPI lifespan via
# :func:`startup` below.
#
# Design constraints from Requirement 6:
#   * client_id prefixed with ``sapa-ai-gate-`` (Req 6.2)
#   * connect with 5 s timeout, then ``loop_start`` (Req 6.5)
#   * publish with 2 s wait_for_publish (Req 6.6)
#   * 1× reconnect retry on publish failure (Req 6.6 + Property 9)
#   * NEVER subscribe — AI Gate is publish-only (Req 6.7 / Property 15)
#   * shutdown → ``loop_stop`` + ``disconnect`` (Req 6.8)
# ---------------------------------------------------------------------------


# Connect timeout is enforced via ``socket.setdefaulttimeout`` for the
# duration of ``connect()``. paho-mqtt 1.6.x reads the default socket
# timeout at the moment the TCP socket is opened, so this is the most
# portable way to bound the call without monkeypatching paho internals.
_MQTT_CONNECT_TIMEOUT_SECONDS = 5.0
_MQTT_PUBLISH_TIMEOUT_SECONDS = 2.0
_MQTT_RECONNECT_TIMEOUT_SECONDS = 2.0
_MQTT_KEEPALIVE_SECONDS = 60


class MqttGatePublisher:
    """Lazy singleton MQTT publisher for the AI Gate router.

    Construction is side-effect-free: the underlying paho client is not
    created until :meth:`startup` is invoked (typically from the FastAPI
    lifespan event). After ``startup`` the publisher exposes a single
    operation, :meth:`publish`, which writes a payload to the configured
    topic and waits for the broker's PUBACK with a 2 s deadline. On
    failure it transparently attempts a single reconnect and re-publish
    before giving up. :meth:`shutdown` releases the network loop and
    socket cleanly.

    The class deliberately does **not** expose ``subscribe()`` — the AI
    Gate is publish-only by design (Requirement 6.7).
    """

    def __init__(
        self,
        broker: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        topic: str = DEFAULT_MQTT_TOPIC_GATE,
        client_id: Optional[str] = None,
    ) -> None:
        # Configuration only — no socket, no thread, no Client object.
        # Honour Requirement 1.3 / Property 19: import + construction
        # MUST be import-side-effect free.
        self._broker = broker
        self._port = port
        self._username = username or None
        self._password = password or ""
        self._topic = topic
        # ``client_id`` is per-process to avoid two backend pods racing
        # for the same MQTT session id. Callers may override the default
        # in tests.
        self._client_id = client_id or f"sapa-ai-gate-{os.getpid()}"

        # Mutable runtime state — populated by :meth:`startup`.
        self._client: Optional[Any] = None
        self._connected: bool = False
        # ``_state_lock`` guards the (client, _connected) pair against
        # races between ``startup``, ``publish``, and ``shutdown``.
        self._state_lock = threading.Lock()

    # -- introspection helpers (used by tests + design property 15) -------

    @property
    def broker(self) -> str:
        return self._broker

    @property
    def port(self) -> int:
        return self._port

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- lifecycle --------------------------------------------------------

    def startup(self) -> None:
        """Build the paho client, connect to the broker, start the loop.

        Synchronous on purpose — paho's ``connect``/``loop_start`` API is
        blocking. The async wrapper :func:`startup` schedules this on a
        thread executor so it does not stall the FastAPI event loop.

        Connection failures are *logged* but never raised: a transient
        broker outage at boot must not crash the backend (Requirement
        1.7). :meth:`publish` will lazily retry via ``reconnect()`` on
        first use.
        """

        with self._state_lock:
            if self._client is not None:
                # Idempotent: a second startup() call is a no-op so
                # tests and the lifespan handler can call it freely.
                return

            client = paho_mqtt_client.Client(client_id=self._client_id)
            if self._username:
                # Empty password is permitted (matches mosquitto auth
                # files where users may have no password).
                client.username_pw_set(self._username, self._password)

            # Bound the connect call to ~5 s. paho-mqtt 1.6.x does not
            # accept a per-call timeout on ``connect``; using
            # ``socket.setdefaulttimeout`` for the duration of the call
            # is the standard work-around.
            previous_default = socket.getdefaulttimeout()
            try:
                socket.setdefaulttimeout(_MQTT_CONNECT_TIMEOUT_SECONDS)
                client.connect(
                    self._broker,
                    self._port,
                    keepalive=_MQTT_KEEPALIVE_SECONDS,
                )
            except (
                OSError,
                socket.timeout,
                socket.gaierror,
                ConnectionError,
                Exception,
            ) as exc:
                # Defensive: we want the backend to come up even if the
                # broker is briefly unreachable. The first ``publish()``
                # call will attempt a reconnect.
                print(
                    f"[ai_gate] MQTT connect failed broker={self._broker} "
                    f"port={self._port}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                # Keep the client object so reconnect() has a target.
                self._client = client
                self._connected = False
                return
            finally:
                socket.setdefaulttimeout(previous_default)

            try:
                client.loop_start()
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"[ai_gate] MQTT loop_start failed: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                self._client = client
                self._connected = False
                return

            self._client = client
            self._connected = True

    def shutdown(self) -> None:
        """Stop the network loop and disconnect cleanly.

        Idempotent: safe to call multiple times. Errors during teardown
        are logged but never raised so they cannot mask the original
        shutdown signal.
        """

        with self._state_lock:
            client = self._client
            if client is None:
                return
            try:
                client.loop_stop()
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"[ai_gate] MQTT loop_stop failed: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            try:
                client.disconnect()
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"[ai_gate] MQTT disconnect failed: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            self._client = None
            self._connected = False

    # -- publish ----------------------------------------------------------

    def publish(
        self,
        topic: str,
        payload: str,
        timeout: float = _MQTT_PUBLISH_TIMEOUT_SECONDS,
    ) -> bool:
        """Publish ``payload`` to ``topic`` with QoS 1 and 2 s PUBACK.

        Returns ``True`` only when paho reports ``rc == 0`` AND
        ``info.wait_for_publish(timeout)`` completes within ``timeout``
        seconds. On any failure (rc != 0, broker down, PUBACK timeout)
        the publisher attempts exactly **one** reconnect with a 2 s
        deadline and re-publishes; if the second attempt also fails
        ``False`` is returned and the caller (face-match handler) is
        expected to escalate to HTTP 503 ``mqtt_unavailable``.

        Never raises — failures are funnelled through the ``False``
        return value so callers never have to wrap calls in try/except.
        """

        client = self._client
        if client is None:
            # ``startup`` has not run (or failed without retaining the
            # client). The face-match handler treats this as an MQTT
            # outage.
            print(
                "[ai_gate] MQTT publish skipped: client not initialised",
                flush=True,
            )
            return False

        ok = self._publish_once(client, topic, payload, timeout)
        if ok:
            return True

        # Single reconnect attempt with its own 2 s deadline.
        if not self._reconnect(client):
            return False

        return self._publish_once(client, topic, payload, timeout)

    def _publish_once(
        self,
        client: Any,
        topic: str,
        payload: str,
        timeout: float,
    ) -> bool:
        """One publish + ``wait_for_publish`` cycle. Never raises."""

        try:
            info = client.publish(topic, payload, qos=1)
        except Exception as exc:
            print(
                f"[ai_gate] MQTT publish raised {type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

        rc = getattr(info, "rc", None)
        if rc != 0:
            print(
                f"[ai_gate] MQTT publish rc={rc} topic={topic!r}",
                flush=True,
            )
            return False

        try:
            info.wait_for_publish(timeout)
        except RuntimeError as exc:
            # paho raises RuntimeError on PUBACK timeout / disconnect.
            print(
                f"[ai_gate] MQTT wait_for_publish timeout topic={topic!r}: "
                f"{exc}",
                flush=True,
            )
            return False
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"[ai_gate] MQTT wait_for_publish raised "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

        # ``is_published`` is the canonical "PUBACK received" indicator
        # in paho-mqtt 1.6; older versions omit it, in which case a
        # successful ``wait_for_publish`` is enough.
        is_published = getattr(info, "is_published", None)
        if callable(is_published):
            try:
                if not is_published():
                    return False
            except Exception:  # pragma: no cover - defensive
                return False
        return True

    def _reconnect(self, client: Any) -> bool:
        """Attempt a single reconnect with a 2 s deadline. Never raises."""

        previous_default = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(_MQTT_RECONNECT_TIMEOUT_SECONDS)
            client.reconnect()
            return True
        except Exception as exc:
            print(
                f"[ai_gate] MQTT reconnect failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False
        finally:
            socket.setdefaulttimeout(previous_default)


# ---------------------------------------------------------------------------
# Singleton accessor (Task 3.3)
#
# A module-level reference holds the lazy singleton. The first call to
# :func:`get_mqtt_publisher` builds the publisher with values resolved
# from the environment via the ``_safe_*`` helpers; subsequent calls
# return the cached instance. Tests reset it by assigning ``None`` to
# the module attribute or by calling :func:`_reset_mqtt_publisher`.
# ---------------------------------------------------------------------------


_mqtt_publisher: Optional[MqttGatePublisher] = None
_mqtt_publisher_lock = threading.Lock()


def get_mqtt_publisher() -> MqttGatePublisher:
    """Return the proc-wide :class:`MqttGatePublisher` singleton.

    The publisher is constructed lazily on first call so importing
    ``ai_gate`` never builds it (Requirement 1.3 / Property 19). All
    configuration is resolved from environment variables at the moment
    of construction via :func:`_safe_port` and :func:`_safe_topic`.

    Note: construction does **not** open a socket — that happens in
    :meth:`MqttGatePublisher.startup` which the lifespan calls.
    """

    global _mqtt_publisher
    with _mqtt_publisher_lock:
        if _mqtt_publisher is None:
            broker = (
                os.environ.get(ENV_MQTT_BROKER, "") or DEFAULT_MQTT_BROKER
            )
            port = _safe_port(ENV_MQTT_PORT, DEFAULT_MQTT_PORT)
            username = (
                os.environ.get(ENV_MQTT_USERNAME, "") or DEFAULT_MQTT_USERNAME
            )
            password = os.environ.get(ENV_MQTT_PASSWORD, "")
            topic = _safe_topic(ENV_MQTT_TOPIC_GATE, DEFAULT_MQTT_TOPIC_GATE)
            _mqtt_publisher = MqttGatePublisher(
                broker=broker,
                port=port,
                username=username or None,
                password=password,
                topic=topic,
            )
        return _mqtt_publisher


def _reset_mqtt_publisher() -> None:
    """Test helper: drop the singleton so the next call rebuilds it."""

    global _mqtt_publisher
    with _mqtt_publisher_lock:
        if _mqtt_publisher is not None:
            try:
                _mqtt_publisher.shutdown()
            except Exception:  # pragma: no cover - defensive
                pass
        _mqtt_publisher = None


# ---------------------------------------------------------------------------
# Attendance + audit logger (Task 3.4)
#
# ``AttendanceLogger`` is the AI Gate's bridge to MongoDB. It is **independent**
# from ``backend.mongo_db`` (Requirement 1.5) and owns its own
# ``pymongo.MongoClient`` configured with ``serverSelectionTimeoutMS=3000``.
# Per Requirement 5.5 the logger is forbidden from touching any collection
# other than ``attendance_logs`` and ``audit_logs`` — Property 13 verifies
# this scope at runtime.
#
# All public methods follow the same contract:
#   * Return a ``dict`` containing at least ``{"logged": bool}`` plus optional
#     metadata (``deduped`` for ``insert_attendance``, ``error`` carrying the
#     exception class name on failure).
#   * Wrap MongoDB operations in ``try/except (PyMongoError, OSError, ...)``
#     so a broker outage degrades gracefully (Requirement 5.3) — failures
#     log a structured line to stdout (``print(..., flush=True)``) and the
#     method returns ``logged=False`` instead of raising.
#   * Build the underlying ``MongoClient`` lazily on first call so importing
#     ``ai_gate`` cannot open a network socket (Requirement 1.3 /
#     Property 19).
# ---------------------------------------------------------------------------


# 3 s server-selection deadline aligns with Requirement 5.3 ("operasi
# penulisan ke MongoDB tidak selesai dalam 3 detik" → fallback path).
_MONGO_SERVER_SELECTION_TIMEOUT_MS = 3000
# Per-call socket timeout — paho-mqtt-style guard so a hung TCP write
# does not exceed the 3 s budget. ``socketTimeoutMS`` is honoured by
# pymongo for every individual operation.
_MONGO_SOCKET_TIMEOUT_MS = 3000
# Dedupe window — a duplicate ``in`` check-in within 5 s of the previous
# one is suppressed (Requirement 5.6).
_ATTENDANCE_DEDUPE_WINDOW_SECONDS = 5


def _utc_now() -> datetime:
    """Return a Pydantic-friendly timezone-aware UTC ``datetime``."""

    return datetime.now(timezone.utc)


class AttendanceLogger:
    """MongoDB writer for ``attendance_logs`` and ``audit_logs``.

    Construction is side-effect-free: the underlying ``MongoClient`` is
    built on the first call to :meth:`_get_db` so importing ``ai_gate``
    cannot hit the network (Requirement 1.3). Configuration is captured
    at construction time so per-process env mutations between calls do
    not split traffic across two clusters.

    Notes:

    * ``MongoClient`` is created with ``serverSelectionTimeoutMS=3000``
      and ``socketTimeoutMS=3000`` so every operation is bounded by the
      3 s budget mandated by Requirement 5.3.
    * The class deliberately exposes **only** :meth:`insert_attendance`,
      :meth:`insert_audit_unknown`, and :meth:`insert_audit_mqtt_failure`
      to enforce Requirement 5.5 / Property 13 at the API level — the
      class never reaches into any other collection.
    * No indexes are created at construction time; schema modification
      is out of scope for this module.
    """

    ATTENDANCE_COLLECTION = "attendance_logs"
    AUDIT_COLLECTION = "audit_logs"
    SOURCE_TAG = "ai_gate"

    def __init__(
        self,
        mongo_uri: str = DEFAULT_MONGODB_URI,
        db_name: str = DEFAULT_MONGODB_DB,
    ) -> None:
        # Configuration only — no MongoClient yet (Requirement 1.3 /
        # Property 19).
        self._mongo_uri = mongo_uri
        self._db_name = db_name
        self._client: Optional[pymongo.MongoClient] = None
        # Guard concurrent first-call client construction so we never
        # build two ``MongoClient`` instances racing on the same URI.
        self._client_lock = threading.Lock()

    # -- introspection helpers -------------------------------------------

    @property
    def mongo_uri(self) -> str:
        return self._mongo_uri

    @property
    def db_name(self) -> str:
        return self._db_name

    # -- internal helpers ------------------------------------------------

    def _get_db(self) -> Any:
        """Return the cached pymongo ``Database`` handle.

        Builds the underlying ``MongoClient`` lazily on first call. The
        client uses ``serverSelectionTimeoutMS=3000`` so every operation
        is bounded by the 3 s budget required by Requirement 5.3.
        """

        with self._client_lock:
            if self._client is None:
                self._client = pymongo.MongoClient(
                    self._mongo_uri,
                    serverSelectionTimeoutMS=_MONGO_SERVER_SELECTION_TIMEOUT_MS,
                    socketTimeoutMS=_MONGO_SOCKET_TIMEOUT_MS,
                )
            return self._client[self._db_name]

    def close(self) -> None:
        """Tear down the underlying ``MongoClient`` if any was built.

        Idempotent — safe to call multiple times.
        """

        with self._client_lock:
            client = self._client
            if client is None:
                return
            try:
                client.close()
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"[ai_gate] AttendanceLogger close failed: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            self._client = None

    # -- attendance -------------------------------------------------------

    def insert_attendance(
        self,
        employee_id: str,
        direction: str = "in",
        confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Insert an attendance row, suppressing duplicates within 5 s.

        The dedupe rule (Requirement 5.6) is: if a document already
        exists in ``attendance_logs`` for the same ``employee_id`` with
        ``status == "present"``, ``direction == "in"``, and a
        ``timestamp`` not older than 5 seconds relative to ``now``,
        skip the insert and return ``{"logged": True, "deduped": True}``
        so the caller can still report ``logged=true`` to the Edge.

        On any ``PyMongoError`` (or transport-level exception) the
        method logs to stdout and returns ``{"logged": False,
        "deduped": False, "error": "<ExcClass>"}`` — the caller is then
        free to continue with the MQTT publish per Requirement 5.3.
        """

        now = _utc_now()
        try:
            collection = self._get_db()[self.ATTENDANCE_COLLECTION]

            # Dedupe check — only meaningful for ``in`` direction
            # because Requirement 5.6 is scoped to the entry side.
            window_start = now - timedelta(
                seconds=_ATTENDANCE_DEDUPE_WINDOW_SECONDS
            )
            existing = collection.find_one(
                {
                    "employee_id": employee_id,
                    "status": "present",
                    "direction": "in",
                    "timestamp": {"$gte": window_start},
                }
            )
            if existing is not None:
                return {"logged": True, "deduped": True}

            document: Dict[str, Any] = {
                "employee_id": employee_id,
                "direction": direction,
                "timestamp": now,
                "status": "present",
                "source": self.SOURCE_TAG,
            }
            if confidence is not None:
                document["confidence"] = confidence
            collection.insert_one(document)
            return {"logged": True, "deduped": False}
        except (PyMongoError, OSError, socket.timeout) as exc:
            print(
                f"[ai_gate] AttendanceLogger.insert_attendance failed "
                f"employee_id={employee_id!r}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return {
                "logged": False,
                "deduped": False,
                "error": type(exc).__name__,
            }

    # -- audit ------------------------------------------------------------

    def insert_audit_unknown(
        self, employee_id: Optional[str]
    ) -> Dict[str, Any]:
        """Record an ``unknown_face`` warning to ``audit_logs``.

        Triggered by Requirement 5.2 when an Edge face-match arrives
        with ``is_valid=false`` (whether or not ``employee_id`` was
        supplied — the field is preserved verbatim, including ``None``).
        """

        now = _utc_now()
        try:
            collection = self._get_db()[self.AUDIT_COLLECTION]
            document: Dict[str, Any] = {
                "event_type": "unknown_face",
                "employee_id": employee_id,
                "status": "warning",
                "timestamp": now,
                "source": self.SOURCE_TAG,
            }
            collection.insert_one(document)
            return {"logged": True}
        except (PyMongoError, OSError, socket.timeout) as exc:
            print(
                f"[ai_gate] AttendanceLogger.insert_audit_unknown failed "
                f"employee_id={employee_id!r}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return {"logged": False, "error": type(exc).__name__}

    def insert_audit_mqtt_failure(
        self,
        action: str,
        employee_id: Optional[str],
    ) -> Dict[str, Any]:
        """Record an ``mqtt_publish_failed`` audit row (Requirement 4.7).

        Called by the face-match handler when the MQTT publisher exhausts
        its 1× reconnect retry. The row carries the ``action`` that was
        about to be published (``"open"`` or ``"invalid"``) plus
        ``employee_id`` when available.
        """

        now = _utc_now()
        try:
            collection = self._get_db()[self.AUDIT_COLLECTION]
            document: Dict[str, Any] = {
                "event_type": "mqtt_publish_failed",
                "action": action,
                "employee_id": employee_id,
                "status": "error",
                "timestamp": now,
                "source": self.SOURCE_TAG,
            }
            collection.insert_one(document)
            return {"logged": True}
        except (PyMongoError, OSError, socket.timeout) as exc:
            print(
                f"[ai_gate] AttendanceLogger.insert_audit_mqtt_failure failed "
                f"action={action!r} employee_id={employee_id!r}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return {"logged": False, "error": type(exc).__name__}


# ---------------------------------------------------------------------------
# Singleton accessor (Task 3.4) — mirrors the MQTT publisher pattern.
# ---------------------------------------------------------------------------


_attendance_logger: Optional[AttendanceLogger] = None
_attendance_logger_lock = threading.Lock()


def get_attendance_logger() -> AttendanceLogger:
    """Return the proc-wide :class:`AttendanceLogger` singleton.

    The logger is constructed lazily on first call — the import path of
    ``ai_gate`` therefore cannot open a MongoDB connection (Requirement
    1.3 / Property 19). Configuration is resolved from the environment
    at construction time via ``MONGODB_URI`` and ``MONGODB_DB`` (with
    the documented defaults).
    """

    global _attendance_logger
    with _attendance_logger_lock:
        if _attendance_logger is None:
            mongo_uri = (
                os.environ.get(ENV_MONGODB_URI, "") or DEFAULT_MONGODB_URI
            )
            db_name = (
                os.environ.get(ENV_MONGODB_DB, "") or DEFAULT_MONGODB_DB
            )
            _attendance_logger = AttendanceLogger(
                mongo_uri=mongo_uri,
                db_name=db_name,
            )
        return _attendance_logger


def _reset_attendance_logger() -> None:
    """Test helper: drop the singleton so the next call rebuilds it."""

    global _attendance_logger
    with _attendance_logger_lock:
        if _attendance_logger is not None:
            try:
                _attendance_logger.close()
            except Exception:  # pragma: no cover - defensive
                pass
        _attendance_logger = None


# ---------------------------------------------------------------------------
# Router (Requirements 1.1, 1.2)
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/edge", tags=["AI Gate"])


# ---------------------------------------------------------------------------
# Module-level singletons used by request handlers
#
# Re-instantiating ``ImageValidator`` / ``JwtVerifier`` / ``FaceStorage``
# on every request would be wasteful — they hold no per-request state and
# read all configuration (env vars, faces dir override) lazily at call
# time. Keeping a single instance per process is cheap and matches the
# "shared, side-effect-free helper" pattern documented in design.md.
# Tests that need to swap implementations can monkeypatch these names.
# ---------------------------------------------------------------------------

_image_validator = ImageValidator()
_jwt_verifier = JwtVerifier()
_edge_key_verifier = EdgeKeyVerifier()
_face_storage = FaceStorage()
_employee_lookup = EmployeeLookup()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/upload-face/{employee_id}",
    response_model=UploadResponse,
)
async def upload_face(
    employee_id: str,
    file: Optional[UploadFile] = File(default=None),
    authorization: Optional[str] = Header(default=None),
) -> UploadResponse:
    """Persist a manager-uploaded face image to ``Faces_Storage``.

    Implements Requirement 2 with the strict validation precedence
    enumerated in Requirement 2.9:

    1. JWT decode (401 ``invalid_token`` / 403 ``forbidden_role``).
    2. ``employee_id`` regex (400 ``invalid_employee_id``).
    3. ``file`` field present and non-empty (400 ``file_field_required``).
    4. Size ≤ 5 MB (413 ``file_too_large``) — enforced via a streaming
       read with a one-byte overshoot so the cap is detected before any
       byte is written to disk.
    5. Content-type whitelist (415 ``unsupported_image_type``).
    6. Magic-byte match against declared content-type
       (415 ``image_content_mismatch``).
    7. Atomic write via :class:`FaceStorage` (500 ``storage_write_failed``
       on ``OSError`` or unrecoverable Pillow failure; tmp file is
       cleaned up).

    PNG inputs are transcoded to JPEG before being persisted, so the
    canonical ``saved_path`` is always ``uploads/faces/{employee_id}.jpg``
    (Requirement 2.2).
    """

    # Step 1 — authentication / authorization. ``JwtVerifier.verify``
    # raises ``HTTPException`` directly with the correct status + detail
    # so we just let it propagate.
    _jwt_verifier.verify(authorization)

    # Step 2 — employee_id format check (Requirement 2.10).
    if not _image_validator.validate_employee_id(employee_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_employee_id",
        )

    # Step 3 — ``file`` field present and non-empty (Requirement 2.12).
    # ``UploadFile`` instances always exist when FastAPI received any
    # multipart part, but tests / clients can omit the field entirely
    # (then ``file`` is ``None``), or send an empty filename.
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file_field_required",
        )
    upload_stream = getattr(file, "file", None)
    if upload_stream is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file_field_required",
        )

    # Step 4 — streaming size check with a one-byte overshoot. Reading
    # ``MAX_SIZE_BYTES + 1`` lets us detect "exactly 5 MB + one byte"
    # without allocating an unbounded buffer. The cap is enforced
    # *before* any bytes are written to disk (Requirement 2.6).
    max_bytes = ImageValidator.MAX_SIZE_BYTES
    blob = await file.read(max_bytes + 1)
    if blob is None:
        # Defensive: some UploadFile shims may return ``None`` on EOF.
        blob = b""
    if not isinstance(blob, (bytes, bytearray)):
        # Defensive: an exotic shim may hand us a string. Normalise so
        # downstream magic-byte checks behave predictably.
        try:
            blob = bytes(blob)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="file_field_required",
            )
    if len(blob) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail="file_too_large",
        )
    if len(blob) == 0:
        # Empty body is treated as missing (Requirement 2.12).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file_field_required",
        )

    # Step 5 — content-type whitelist (Requirement 2.4). The validator
    # returns the canonical kind ("jpeg" / "png") or ``None``.
    expected_kind = _image_validator.validate_content_type(
        getattr(file, "content_type", None)
    )
    if expected_kind is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="unsupported_image_type",
        )

    # Step 6 — magic-byte match (Requirement 2.5). Declared and actual
    # encoding MUST agree before we touch the disk.
    if not _image_validator.validate_magic_bytes(blob, expected_kind):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="image_content_mismatch",
        )

    # Step 7 — atomic write (Requirement 2.3, 2.11, 2.13). FaceStorage
    # creates the directory if needed and removes the ``.tmp`` artefact
    # on any failure. Both ``OSError`` (filesystem) and ``RuntimeError``
    # (e.g. Pillow missing for the PNG path) map to HTTP 500
    # ``storage_write_failed`` per Requirement 2.13.
    try:
        _face_storage.write_jpeg(employee_id, bytes(blob), expected_kind)
    except OSError as exc:
        print(
            f"[ai_gate] upload-face storage_write_failed "
            f"employee_id={employee_id!r}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="storage_write_failed",
        )
    except RuntimeError as exc:
        # Pillow unavailable for PNG path, or a corrupt PNG that Pillow
        # refused to decode. Either way the bytes never reached the
        # canonical path so the operation is safe to surface as 500.
        print(
            f"[ai_gate] upload-face storage_write_failed "
            f"employee_id={employee_id!r}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="storage_write_failed",
        )

    return UploadResponse(
        employee_id=employee_id,
        saved_path=f"uploads/faces/{employee_id}.jpg",
    )


@router.get(
    "/faces",
    response_model=FacesListResponse,
)
async def list_faces(
    x_edge_key: Optional[str] = Header(default=None, alias="X-EDGE-KEY"),
    authorization: Optional[str] = Header(default=None),
) -> FacesListResponse:
    """Return the deterministic ``Faces_Storage`` listing for Edge_Laptop.

    Implements Requirement 3 with the dual-auth precedence demanded by
    Requirement 3.6 (``X-EDGE-KEY`` *or* a Manager/Admin JWT) and the
    misconfiguration / failure modes from 3.7–3.9:

    * ``X-EDGE-KEY`` header matches ``EDGE_INGEST_KEY`` env → 200
      (constant-time compare via :class:`EdgeKeyVerifier`).
    * Else, ``Authorization: Bearer <jwt>`` decodes successfully with a
      role in ``{manager, admin}`` → 200 (delegated to
      :class:`JwtVerifier`).
    * ``EDGE_INGEST_KEY`` env empty AND no ``Authorization`` header →
      503 ``edge_auth_misconfigured`` (Requirement 3.8).
    * Any other auth failure → 401 ``edge_auth_required``
      (Requirement 3.7); we deliberately collapse a 403 from the JWT
      role check into the same 401 so the dual-auth surface does not
      leak which path was tried.
    * Listing succeeds → 200 ``{"faces": [...]}`` sorted lexicographically
      by stem (Requirements 3.1, 3.2, 3.5).
    * Directory missing or empty → 200 ``{"faces": []}``
      (Requirement 3.3).
    * ``OSError`` while reading the directory → 500
      ``faces_storage_unreadable`` (Requirement 3.9).
    """

    edge_key_configured = bool(os.environ.get(ENV_EDGE_INGEST_KEY, "") or "")
    edge_key_ok = _edge_key_verifier.check(x_edge_key)

    jwt_ok = False
    if not edge_key_ok and authorization:
        try:
            _jwt_verifier.verify(authorization)
            jwt_ok = True
        except HTTPException:
            # ``JwtVerifier`` raises 401 ``invalid_token`` for bad tokens
            # and 403 ``forbidden_role`` for non-manager/admin claims.
            # Either branch must collapse into the dual-auth 401 below
            # so we do not leak which auth method was attempted.
            jwt_ok = False

    if not edge_key_ok and not jwt_ok:
        # Requirement 3.8: when no edge key is configured AND the
        # caller cannot present a valid Manager/Admin JWT, the service
        # is misconfigured rather than the caller being unauthorised.
        # An invalid/expired/wrong-role JWT counts as "no valid JWT"
        # (``jwt_ok`` is ``False``), so the 503 path triggers whether
        # ``Authorization`` was absent or merely unusable.
        if not edge_key_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="edge_auth_misconfigured",
            )
        # Requirement 3.7: env is configured but the caller did not
        # match either auth path — plain 401.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="edge_auth_required",
        )

    # Auth ok — list ``Faces_Storage`` deterministically. The storage
    # helper already filters by case-insensitive extension and returns
    # the empty list when the directory is missing (Requirement 3.3),
    # so we only need to translate ``OSError`` into 500
    # ``faces_storage_unreadable`` (Requirement 3.9).
    try:
        entries = _face_storage.list_faces()
    except OSError as exc:
        print(
            f"[ai_gate] list_faces faces_storage_unreadable: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="faces_storage_unreadable",
        )

    faces = [
        FaceListItem(
            employee_id=stem,
            url=f"/api/static/faces/{filename}",
        )
        for stem, filename in entries
    ]
    return FacesListResponse(faces=faces)


# ---------------------------------------------------------------------------
# Face-match endpoint (Task 7.1)
#
# The handler implements the strict request-processing order mandated by
# Requirements 4 and 5:
#
#   1. ``X-EDGE-KEY`` constant-time check (401 ``edge_auth_required``).
#      No MQTT, no MongoDB, no SQL touched.
#   2. Pydantic body validation with ``extra="forbid"``. Because Pydantic
#      validation runs *before* the handler when a model is bound to the
#      signature, we instead accept a raw ``Request`` and call
#      ``FaceMatchRequest.model_validate_json`` ourselves so the auth
#      check can reliably precede body validation. ``ValidationError`` →
#      422 with the original Pydantic detail; malformed JSON → 422 with
#      ``invalid_json``. No MQTT, no MongoDB.
#   3. Cross-field semantic check: ``is_valid=True`` requires a
#      non-empty ``employee_id`` (400 ``employee_id_required_when_valid``).
#      No MQTT, no MongoDB.
#   4. Best-effort employee existence lookup against Postgres via
#      :class:`EmployeeLookup`. Failures yield ``None`` and never block.
#   5. MongoDB write — ``insert_attendance`` (with 5 s dedupe window)
#      for ``is_valid=True`` and ``insert_audit_unknown`` for
#      ``is_valid=False``. Failures degrade to ``logged=False``
#      (Requirement 5.3) without aborting the publish step. Only when
#      both Mongo and the stdout fallback fail do we surface 500
#      ``logging_unavailable`` (Requirement 5.4).
#   6. MQTT publish to ``MQTT_TOPIC_GATE`` with payload
#      ``{"action": "open"|"invalid", "employee_id": id|null,
#      "ts": "<ISO-8601 ms Z>"}``. Failure (after the publisher's
#      internal 1× reconnect retry) → 503 ``mqtt_unavailable`` AND a
#      best-effort ``insert_audit_mqtt_failure`` row.
#
# Successful path returns 200 with the canonical
# ``FaceMatchResponse(action, employee_id, logged)`` body, with the
# whole flow bounded to ≤ 3 s by the inner timeouts (Mongo 3 s socket
# budget, MQTT 2 s publish + 2 s reconnect).
# ---------------------------------------------------------------------------


def _iso8601_utc_milliseconds() -> str:
    """Return the current UTC time as ``YYYY-MM-DDThh:mm:ss.fffZ``.

    Wraps the timestamp construction mandated by Requirement 4.1/4.2 so
    the handler stays readable. Calls
    ``datetime.now(timezone.utc).isoformat(timespec='milliseconds')`` and
    swaps the trailing ``+00:00`` for the canonical ``Z`` suffix.
    """

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@router.post(
    "/face-match",
    response_model=FaceMatchResponse,
)
async def face_match(
    request: Request,
    x_edge_key: Optional[str] = Header(default=None, alias="X-EDGE-KEY"),
) -> FaceMatchResponse:
    """Bridge an Edge face-match decision to the ESP32 gate via MQTT.

    See the module-level commentary above for the strict ordering this
    handler must obey. Each numbered step below corresponds to a numbered
    bullet in that block. Side-effecting steps (MongoDB writes, MQTT
    publish) are entered *only* after every preceding validation step
    has succeeded.
    """

    # ------------------------------------------------------------------
    # Step 1 — X-EDGE-KEY (Requirement 4.11). MUST run before any body
    # parse so a missing/bad header never reveals whether the body was
    # well-formed (no MQTT, no Mongo, no SQL).
    # ------------------------------------------------------------------
    if not _edge_key_verifier.check(x_edge_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="edge_auth_required",
        )

    # ------------------------------------------------------------------
    # Step 2 — Pydantic body validation with ``extra="forbid"``
    # (Requirements 4.4, 4.5). We do this *manually* (instead of binding
    # the model to the signature) so step 1 above can run first; FastAPI
    # would otherwise raise 422 before our handler executes. Mirrors the
    # Pydantic detail format ``{"detail": [...]}`` so existing API
    # consumers do not see a contract break.
    # ------------------------------------------------------------------
    raw_body = await request.body()
    try:
        payload = FaceMatchRequest.model_validate_json(raw_body)
    except ValidationError as exc:
        # ``include_input=False`` keeps the response JSON-serialisable
        # (Pydantic embeds the raw input — possibly bytes — in each
        # error otherwise). ``include_url=False`` and
        # ``include_context=False`` trim the rest of the noise so the
        # 422 body matches the format a model-bound endpoint would emit.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(
                include_input=False,
                include_url=False,
                include_context=False,
            ),
        )
    except ValueError:
        # Defensive fallback for older Pydantic builds that may surface
        # syntactically invalid JSON as a plain ``ValueError``.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_json",
        )

    # ------------------------------------------------------------------
    # Step 3 — semantic cross-field check (Requirement 4.6). Pydantic's
    # ``min_length=1`` already rejects ``employee_id=""`` with a 422,
    # but ``employee_id=None`` paired with ``is_valid=True`` is shape-
    # valid yet semantically wrong → 400 here.
    # ------------------------------------------------------------------
    if payload.is_valid and not payload.employee_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="employee_id_required_when_valid",
        )

    # ------------------------------------------------------------------
    # Step 4 — best-effort Postgres lookup. Per design.md the lookup is
    # advisory only: we log the result for ops visibility but never let
    # it block a gate decision (a brief Postgres outage MUST not stop
    # legitimate openings). ``EmployeeLookup.exists`` already swallows
    # SQLAlchemy errors and returns ``None`` in that case.
    # ------------------------------------------------------------------
    if payload.employee_id:
        try:
            _employee_lookup.exists(payload.employee_id)
        except Exception as exc:  # pragma: no cover - defensive
            # ``EmployeeLookup`` already catches SQLAlchemyError; any
            # other escape is unexpected but must not abort the request.
            print(
                f"[ai_gate] face-match employee lookup raised "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    action: Literal["open", "invalid"] = (
        "open" if payload.is_valid else "invalid"
    )

    # ------------------------------------------------------------------
    # Step 5 — MongoDB write. Requirement 5.3 demands that a Mongo
    # outage degrade gracefully: we still publish to MQTT and return
    # 200 with ``logged=False``. ``AttendanceLogger`` already wraps its
    # I/O in try/except so a ``logged=False`` result already includes
    # the best-effort stdout fallback. We treat any unexpected escape
    # as a hard failure → 500 ``logging_unavailable`` (Requirement 5.4)
    # before any side-effect can leak to MQTT.
    # ------------------------------------------------------------------
    try:
        attendance_logger = get_attendance_logger()
        if payload.is_valid:
            assert payload.employee_id is not None  # narrowed at step 3
            log_result = attendance_logger.insert_attendance(
                payload.employee_id,
                payload.direction or "in",
                payload.confidence,
            )
        else:
            log_result = attendance_logger.insert_audit_unknown(
                payload.employee_id
            )
    except Exception as exc:
        # Both Mongo write *and* the stdout fallback failed — without a
        # working audit trail we cannot honour Requirement 5.4.
        print(
            f"[ai_gate] face-match logging_unavailable "
            f"action={action} employee_id={payload.employee_id!r}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="logging_unavailable",
        )

    if not isinstance(log_result, dict):  # pragma: no cover - defensive
        log_result = {"logged": False}
    logged = bool(log_result.get("logged", False))

    # ------------------------------------------------------------------
    # Step 6 — MQTT publish. ``MqttGatePublisher.publish`` already
    # implements the 1× reconnect retry mandated by Requirements 4.7
    # and 4.8 internally, so a ``False`` return means both attempts
    # exhausted. On failure we record ``mqtt_publish_failed`` to the
    # audit log on a best-effort basis (Requirement 4.7) and surface
    # 503 ``mqtt_unavailable`` (Requirement 4.9: 503 wins regardless of
    # whether the audit row was actually written).
    # ------------------------------------------------------------------
    ts = _iso8601_utc_milliseconds()
    mqtt_payload = json.dumps(
        {
            "action": action,
            "employee_id": payload.employee_id,
            "ts": ts,
        }
    )
    topic = _safe_topic(ENV_MQTT_TOPIC_GATE, DEFAULT_MQTT_TOPIC_GATE)

    try:
        publisher = get_mqtt_publisher()
        publish_ok = publisher.publish(topic, mqtt_payload)
    except Exception as exc:
        # ``publish`` is documented to never raise, but be defensive so
        # the request still terminates with a clean 503 instead of a
        # generic 500 leaking the broker exception class.
        print(
            f"[ai_gate] face-match MQTT publish raised "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        publish_ok = False

    if not publish_ok:
        # Best-effort audit row — failures here are logged but do not
        # change the 503 status (Requirement 4.9).
        try:
            get_attendance_logger().insert_audit_mqtt_failure(
                action,
                payload.employee_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"[ai_gate] face-match audit_mqtt_failure raised "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="mqtt_unavailable",
        )

    return FaceMatchResponse(
        action=action,
        employee_id=payload.employee_id,
        logged=logged,
    )


# ---------------------------------------------------------------------------
# Static faces validation router (Task 8.1 / Requirement 7)
#
# ``static_faces_router`` is a *separate* APIRouter mounted at
# ``/api/static/faces`` so that ``backend/main.py`` can ``include_router``
# it BEFORE registering the fallback ``StaticFiles`` mount at the same
# prefix. FastAPI matches routers in registration order, so the regex /
# traversal validation in this router runs first and the `StaticFiles`
# mount only handles methods this router did not register
# (Requirement 7.6 → 405).
#
# Validation precedence (Requirement 7.3 / 7.5 / 7.4):
#   1. Filename regex ``^[A-Za-z0-9_-]{1,64}\.(jpg|jpeg|png)$``
#      → 400 ``invalid_filename`` on mismatch. NO file is opened.
#   2. Traversal blocklist (substring match, case-insensitive on the
#      ``%`` sequences): ``../``, ``..\\``, ``%2e%2e%2f``, ``%2e%2e%5c``,
#      ``%2f``, ``%5c`` → 404 ``not_found``. Body is sanitised — no
#      path / directory information is leaked.
#   3. ``os.path.realpath`` containment: the resolved path MUST start
#      with the realpath of ``Faces_Storage``; otherwise 404
#      ``not_found``.
#   4. Existence + ``is_file`` check → 404 ``not_found`` if absent.
#   5. ``FileResponse`` with explicit ``media_type`` derived from the
#      extension (``image/jpeg`` for ``.jpg`` / ``.jpeg``, ``image/png``
#      for ``.png``). Starlette sets ``Content-Length`` automatically.
# ---------------------------------------------------------------------------


# Compiled once at import — the regex itself is cheap, but caching it
# keeps the hot path allocation-free.
_STATIC_FACES_FILENAME_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]{1,64}\.(jpg|jpeg|png)$",
    re.IGNORECASE,
)

# Path traversal sequences blocked by Requirement 7.5. The ``%`` sequences
# are matched case-insensitively (HTTP percent-encoding is case-insensitive
# per RFC 3986 §6.2.2.1) so ``%2E%2E%2F`` is rejected just like
# ``%2e%2e%2f``. The plain ``../`` and ``..\\`` are matched verbatim — a
# decoded traversal is what FastAPI hands us when the URL contains ``%2e``
# sequences that the framework already percent-decoded, so we cover both
# the raw form (FastAPI rarely normalises ``../`` away) and the
# percent-encoded form (which Starlette delivers unchanged inside the path
# segment).
_STATIC_FACES_TRAVERSAL_PLAIN = ("../", "..\\")
_STATIC_FACES_TRAVERSAL_ENCODED = (
    "%2e%2e%2f",
    "%2e%2e%5c",
    "%2f",
    "%5c",
)

# Extension → MIME type mapping for ``Content-Type`` (Requirement 7.2).
_STATIC_FACES_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _contains_traversal_sequence(filename: str) -> bool:
    """Return True if ``filename`` contains a known traversal sequence.

    The check is intentionally substring-based: any occurrence of
    ``../`` / ``..\\`` (verbatim) or the percent-encoded forms
    ``%2e%2e%2f`` / ``%2e%2e%5c`` / ``%2f`` / ``%5c``
    (case-insensitive) anywhere in ``filename`` is rejected. The regex
    in step 1 already excludes most of these, but we run this check
    independently so a future relaxation of the regex cannot
    accidentally re-introduce a traversal vulnerability.
    """

    if not isinstance(filename, str):
        return True
    for seq in _STATIC_FACES_TRAVERSAL_PLAIN:
        if seq in filename:
            return True
    lowered = filename.lower()
    for seq in _STATIC_FACES_TRAVERSAL_ENCODED:
        if seq in lowered:
            return True
    return False


static_faces_router = APIRouter(
    prefix="/api/static/faces",
    tags=["AI Gate Static"],
)


@static_faces_router.api_route("/{filename}", methods=["GET", "HEAD"])
async def serve_face(filename: str) -> FileResponse:
    """Serve a single face image from ``Faces_Storage`` after validation.

    The handler implements the public, no-auth contract required by
    Requirement 7. The validation order is fixed (regex → traversal
    blocklist → realpath containment → existence) so any single
    request can only trigger the *first* failure that applies. No
    filesystem I/O happens before step 3; step 1 and step 2 are pure
    string checks.

    Returns:
        :class:`fastapi.responses.FileResponse` with the correct
        ``Content-Type`` (``image/jpeg`` or ``image/png``) and an
        automatically-set ``Content-Length`` reflecting the byte size
        on disk. Starlette's ``FileResponse`` natively handles GET
        and HEAD: HEAD requests get the same headers but an empty body.

    Raises:
        :class:`fastapi.HTTPException`:

        * 400 ``invalid_filename`` — regex mismatch (no I/O).
        * 404 ``not_found`` — traversal sequence / outside
          ``Faces_Storage`` / file missing. The detail is intentionally
          the same generic string so a probing client cannot
          distinguish a "wrong directory" from a "not present"
          failure.
    """

    # Step 1 — regex validation (Requirement 7.3). NO file is opened.
    if not isinstance(filename, str) or not _STATIC_FACES_FILENAME_PATTERN.match(
        filename
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_filename",
        )

    # Step 2 — traversal blocklist (Requirement 7.5). Generic 404 with
    # no path information so the response cannot be used as an oracle
    # for the storage layout.
    if _contains_traversal_sequence(filename):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not_found",
        )

    # Step 3 — realpath containment (Requirement 7.5). We resolve both
    # the candidate path and the storage root via ``os.path.realpath``
    # (which follows symlinks) and require the candidate to live
    # underneath the root. ``os.path.commonpath`` is avoided because
    # it raises ``ValueError`` for paths on different drives on
    # Windows; an explicit ``startswith`` check on a path with a
    # trailing ``os.sep`` is portable and unambiguous.
    faces_dir = _face_storage.faces_dir
    faces_root = os.path.realpath(str(faces_dir))
    candidate = os.path.realpath(os.path.join(faces_root, filename))
    # Append os.sep to the root before comparison so the file at
    # ``/foo/bar`` cannot pass containment for root ``/foo/ba``.
    root_with_sep = faces_root.rstrip(os.sep) + os.sep
    if candidate != faces_root and not candidate.startswith(root_with_sep):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not_found",
        )

    # Step 4 — existence + regular-file check (Requirement 7.4). Same
    # generic ``not_found`` detail to avoid leaking storage internals.
    if not os.path.exists(candidate) or not os.path.isfile(candidate):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not_found",
        )

    # Step 5 — success: explicit media type from extension. Starlette
    # populates ``Content-Length`` from ``os.stat`` automatically and
    # handles HEAD by returning the headers with an empty body.
    ext = os.path.splitext(candidate)[1].lower()
    media_type = _STATIC_FACES_MEDIA_TYPES.get(ext, "application/octet-stream")
    return FileResponse(candidate, media_type=media_type)


# ---------------------------------------------------------------------------
# Lifecycle stubs
#
# These coroutines are placeholders to be filled in by later tasks
# (MQTT connect / MongoDB client init in ``startup``; ``loop_stop`` +
# ``disconnect`` in ``shutdown``). They live as module-level coroutines so
# that ``backend.main`` can hook them into its FastAPI lifespan without
# importing anything else from this module.
# ---------------------------------------------------------------------------


async def startup() -> None:
    """Initialise side-effectful resources (MQTT, MongoDB).

    Builds the lazy :class:`MqttGatePublisher` singleton and runs its
    blocking ``connect`` + ``loop_start`` sequence on the default thread
    pool so it never stalls the FastAPI event loop. Failures inside
    ``MqttGatePublisher.startup`` are logged (not raised) so the backend
    keeps booting even when Mosquitto is briefly unreachable
    (Requirement 1.7).
    """

    publisher = get_mqtt_publisher()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, publisher.startup)


async def shutdown() -> None:
    """Release side-effectful resources (MQTT loop, MongoDB client).

    Tears down the :class:`MqttGatePublisher` singleton if one was
    created during startup. Idempotent — calling twice is a no-op.
    """

    global _mqtt_publisher
    publisher = _mqtt_publisher
    if publisher is None:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, publisher.shutdown)


__all__ = [
    "router",
    "static_faces_router",
    "startup",
    "shutdown",
    "FaceMatchRequest",
    "FaceMatchResponse",
    "FaceListItem",
    "FacesListResponse",
    "UploadResponse",
    "ImageValidator",
    "JwtVerifier",
    "EdgeKeyVerifier",
    "FaceStorage",
    "EmployeeLookup",
    "MqttGatePublisher",
    "get_mqtt_publisher",
    "AttendanceLogger",
    "get_attendance_logger",
    "_safe_port",
    "_safe_topic",
    "ENV_MQTT_BROKER",
    "ENV_MQTT_PORT",
    "ENV_MQTT_USERNAME",
    "ENV_MQTT_PASSWORD",
    "ENV_MQTT_TOPIC_GATE",
    "ENV_MONGODB_URI",
    "ENV_MONGODB_DB",
    "ENV_EDGE_INGEST_KEY",
    "ENV_SECRET_KEY",
    "ENV_DATABASE_URL",
    "DEFAULT_MQTT_BROKER",
    "DEFAULT_MQTT_PORT",
    "DEFAULT_MQTT_USERNAME",
    "DEFAULT_MQTT_PASSWORD",
    "DEFAULT_MQTT_TOPIC_GATE",
    "DEFAULT_MONGODB_URI",
    "DEFAULT_MONGODB_DB",
]
