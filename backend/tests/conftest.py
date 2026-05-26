"""Pytest fixtures for AI Gate Edge Integration tests.

This conftest provides reusable fixtures for testing the (forthcoming)
``backend.ai_gate`` module and its integration with ``backend.main``:

* ``test_env`` — autouse env-var setup used by every test.
* ``faces_dir`` — a fresh ``Faces_Storage`` directory under ``tmp_path``.
* ``mqtt_stub`` — a recording paho-mqtt stub (configurable failure modes).
* ``mongo_stub`` — a ``mongomock.MongoClient`` plus monkeypatched factory.
* ``jwt_factory`` — builds JWTs signed with the test ``SECRET_KEY``.
* ``client`` — ``fastapi.testclient.TestClient`` over ``backend.main.app``.

Notes
-----
* ``backend.ai_gate`` is intentionally **not** imported here; it does not
  exist yet at this point of the implementation plan. Any reference is made
  lazily through ``importlib`` inside the test bodies that need it.
* The MQTT and Mongo stubs are installed via ``pytest.MonkeyPatch`` so they
  uninstall automatically at the end of each test.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Optional

import pytest

# ---------------------------------------------------------------------------
# Test-time defaults for environment variables.
#
# These values are intentionally fixed strings so that tests can sign JWTs,
# compare ``X-EDGE-KEY`` headers, and connect a mongomock client without
# touching any real infrastructure. Individual tests are free to override
# any of these via ``monkeypatch.setenv`` / ``monkeypatch.delenv`` — the
# autouse ``test_env`` fixture only sets a baseline.
# ---------------------------------------------------------------------------
TEST_SECRET_KEY = "ai-gate-edge-integration-test-secret"
TEST_EDGE_INGEST_KEY = "test-edge-ingest-key"
TEST_MQTT_BROKER = "127.0.0.1"
TEST_MQTT_PORT = "31883"
TEST_MQTT_USERNAME = "backend"
TEST_MQTT_PASSWORD = "test-password"
TEST_MQTT_TOPIC_GATE = "sapa/gate"
TEST_MONGODB_URI = "mongodb://localhost:27017"
TEST_MONGODB_DB = "sapa_test"
TEST_DATABASE_URL = "sqlite:///:memory:"

_BACKEND_ENV_KEYS = (
    "SECRET_KEY",
    "EDGE_INGEST_KEY",
    "MQTT_BROKER",
    "MQTT_PORT",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "MQTT_TOPIC_GATE",
    "MONGODB_URI",
    "MONGODB_DB",
    "DATABASE_URL",
)


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def test_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set deterministic env values for every test.

    Autouse so that even tests that do not request the fixture explicitly
    still run with a well-known configuration. Returns the dict of values
    set so a test can read what was applied.
    """
    values = {
        "SECRET_KEY": TEST_SECRET_KEY,
        "EDGE_INGEST_KEY": TEST_EDGE_INGEST_KEY,
        "MQTT_BROKER": TEST_MQTT_BROKER,
        "MQTT_PORT": TEST_MQTT_PORT,
        "MQTT_USERNAME": TEST_MQTT_USERNAME,
        "MQTT_PASSWORD": TEST_MQTT_PASSWORD,
        "MQTT_TOPIC_GATE": TEST_MQTT_TOPIC_GATE,
        "MONGODB_URI": TEST_MONGODB_URI,
        "MONGODB_DB": TEST_MONGODB_DB,
        "DATABASE_URL": TEST_DATABASE_URL,
    }
    for key, val in values.items():
        monkeypatch.setenv(key, val)
    return values


# ---------------------------------------------------------------------------
# Filesystem fixture for Faces_Storage
# ---------------------------------------------------------------------------
@pytest.fixture
def faces_dir(tmp_path: Path) -> Path:
    """Return a fresh, empty ``Faces_Storage`` directory under tmp_path.

    The directory is created eagerly so tests that exercise listing logic
    can rely on its existence; tests that need to assert "directory does
    not exist" can ``shutil.rmtree`` it before calling the endpoint.
    """
    target = tmp_path / "uploads" / "faces"
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# MQTT stub
# ---------------------------------------------------------------------------
class MqttStubMessageInfo:
    """Mimic of ``paho.mqtt.client.MQTTMessageInfo`` for tests.

    Exposes ``rc``, ``mid``, and ``wait_for_publish(timeout)``. Behaviour
    is fully driven by attributes set by the test via ``MqttStub`` knobs.
    """

    def __init__(self, rc: int = 0, simulate_timeout: bool = False) -> None:
        self.rc = rc
        self.mid = 1
        self._simulate_timeout = simulate_timeout
        self._published = not simulate_timeout

    def is_published(self) -> bool:
        return self._published

    def wait_for_publish(self, timeout: Optional[float] = None) -> None:
        """If a timeout is being simulated, raise the same error paho does."""
        if self._simulate_timeout:
            # paho raises RuntimeError on timeout when wait_for_publish times out.
            raise RuntimeError("publish wait timed out")
        return None


class MqttStub:
    """Recording stand-in for ``paho.mqtt.client.Client``.

    The stub records every call to ``connect``, ``publish``, ``loop_start``,
    ``loop_stop``, ``disconnect``, ``subscribe``, and ``reconnect`` in
    ``self.calls`` so tests can assert on the exact sequence. It also keeps
    structured event lists (``connects``, ``publishes`` …) for richer
    assertions.

    Failure-mode knobs (mutate freely between calls):

    * ``connect_rc`` — non-zero rc raised from ``connect``/``reconnect``.
    * ``publish_rc`` — non-zero rc returned by ``publish``.
    * ``simulate_publish_timeout`` — ``wait_for_publish`` raises RuntimeError.
    * ``raise_on_connect`` — exception raised from ``connect``.
    * ``raise_on_publish`` — exception raised from ``publish``.
    """

    def __init__(self, *, client_id: str = "", **_: Any) -> None:
        self.client_id = client_id
        self.calls: list[tuple[str, tuple, dict]] = []
        self.connects: list[dict[str, Any]] = []
        self.publishes: list[dict[str, Any]] = []
        self.subscribes: list[Any] = []
        self.username: Optional[str] = None
        self.password: Optional[str] = None
        self.loop_started = False
        self.connected = False
        self._lock = Lock()

        # Failure-mode knobs (test-mutable).
        self.connect_rc: int = 0
        self.publish_rc: int = 0
        self.simulate_publish_timeout: bool = False
        self.raise_on_connect: Optional[BaseException] = None
        self.raise_on_publish: Optional[BaseException] = None

        # paho exposes these so producer code can subscribe to events.
        self.on_connect: Optional[Callable[..., Any]] = None
        self.on_message: Optional[Callable[..., Any]] = None
        self.on_disconnect: Optional[Callable[..., Any]] = None
        self.on_publish: Optional[Callable[..., Any]] = None

    # ------------------------------------------------------------------ helpers
    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        with self._lock:
            self.calls.append((name, args, kwargs))

    # ------------------------------------------------------------------ paho API
    def username_pw_set(self, username: Optional[str], password: Optional[str] = None) -> None:
        self._record("username_pw_set", (username, password), {})
        self.username = username
        self.password = password

    def connect(self, host: str, port: int = 1883, keepalive: int = 60, *args: Any, **kwargs: Any) -> int:
        self._record("connect", (host, port, keepalive, *args), kwargs)
        self.connects.append({"host": host, "port": port, "keepalive": keepalive})
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if self.connect_rc != 0:
            return self.connect_rc
        self.connected = True
        return 0

    def reconnect(self) -> int:
        self._record("reconnect", (), {})
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if self.connect_rc != 0:
            return self.connect_rc
        self.connected = True
        return 0

    def loop_start(self) -> int:
        self._record("loop_start", (), {})
        self.loop_started = True
        return 0

    def loop_stop(self, force: bool = False) -> int:
        self._record("loop_stop", (force,), {})
        self.loop_started = False
        return 0

    def disconnect(self) -> int:
        self._record("disconnect", (), {})
        self.connected = False
        return 0

    def subscribe(self, topic: Any, qos: int = 0, *args: Any, **kwargs: Any) -> tuple[int, int]:
        self._record("subscribe", (topic, qos, *args), kwargs)
        self.subscribes.append(topic)
        return (0, 1)

    def publish(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False) -> MqttStubMessageInfo:
        self._record("publish", (topic, payload, qos, retain), {})
        self.publishes.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        return MqttStubMessageInfo(rc=self.publish_rc, simulate_timeout=self.simulate_publish_timeout)


@pytest.fixture
def mqtt_stub(monkeypatch: pytest.MonkeyPatch) -> MqttStub:
    """Install :class:`MqttStub` in place of ``paho.mqtt.client.Client``.

    The same stub instance is returned by every ``Client(...)`` call within
    the test, so producer code that holds onto the client gets the same
    object the test inspects.
    """
    import paho.mqtt.client as paho_client

    stub = MqttStub()

    def _factory(*args: Any, **kwargs: Any) -> MqttStub:
        # Capture the client_id passed by producer code for visibility.
        if args:
            stub.client_id = stub.client_id or str(args[0])
        if "client_id" in kwargs and not stub.client_id:
            stub.client_id = str(kwargs["client_id"])
        return stub

    monkeypatch.setattr(paho_client, "Client", _factory)
    return stub


# ---------------------------------------------------------------------------
# MongoDB stub (mongomock)
# ---------------------------------------------------------------------------
@pytest.fixture
def mongo_stub(monkeypatch: pytest.MonkeyPatch):
    """Yield a configured ``mongomock.MongoClient`` for the test.

    Patches ``pymongo.MongoClient`` so any code path that constructs a
    fresh client (e.g. ``ai_gate.AttendanceLogger``) receives the same
    in-memory instance. Also patches ``backend.mongo_db._client`` so the
    legacy backend code path uses the same store, keeping tests cohesive.
    """
    mongomock = pytest.importorskip("mongomock")
    import pymongo

    client = mongomock.MongoClient()

    def _factory(*_args: Any, **_kwargs: Any):
        return client

    monkeypatch.setattr(pymongo, "MongoClient", _factory)

    # Reset and patch the backend's cached client if backend.mongo_db
    # has already been imported by another fixture or test.
    if "backend.mongo_db" in sys.modules:
        backend_mongo = sys.modules["backend.mongo_db"]
        monkeypatch.setattr(backend_mongo, "_client", client, raising=False)

    yield client


# ---------------------------------------------------------------------------
# JWT factory
# ---------------------------------------------------------------------------
@pytest.fixture
def jwt_factory() -> Callable[..., str]:
    """Return a callable that builds JWTs signed with the test SECRET_KEY.

    Usage::

        token = jwt_factory(role="manager")
        token = jwt_factory(role="admin", username="alice", expires_in=60)
        token = jwt_factory(role="manager", expired=True)
    """
    from jose import jwt as _jwt

    def _make(
        role: str = "manager",
        *,
        username: str = "tester",
        user_id: int = 1,
        expires_in: int = 30 * 60,
        expired: bool = False,
        extra_claims: Optional[dict[str, Any]] = None,
        secret_key: Optional[str] = None,
        algorithm: str = "HS256",
    ) -> str:
        now = datetime.now(timezone.utc)
        if expired:
            exp = now - timedelta(seconds=60)
        else:
            exp = now + timedelta(seconds=expires_in)
        claims: dict[str, Any] = {
            "sub": username,
            "role": role,
            "uid": user_id,
            "exp": exp,
        }
        if extra_claims:
            claims.update(extra_claims)
        key = secret_key if secret_key is not None else os.getenv("SECRET_KEY", TEST_SECRET_KEY)
        return _jwt.encode(claims, key, algorithm=algorithm)

    return _make


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------
@pytest.fixture
def client(test_env, mqtt_stub, mongo_stub):
    """Return ``TestClient(app)`` for ``backend.main``.

    Depends on ``test_env`` so env vars are set *before* importing
    ``backend.main`` (the module reads ``MQTT_*`` etc. at import time).
    Also depends on ``mqtt_stub`` and ``mongo_stub`` so that import-time
    side effects (paho ``connect`` / pymongo ``MongoClient``) hit the
    in-memory stand-ins instead of real network sockets.

    The fixture forces a fresh import of ``backend.main`` per test so
    module-level state (mqtt client, gate state, edge state) does not
    leak between tests.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    # Drop any previously imported backend.* so module-level connects
    # are re-executed against the active stubs.
    for mod_name in list(sys.modules):
        if mod_name == "backend.main" or mod_name.startswith("backend.main."):
            sys.modules.pop(mod_name, None)

    backend_main = importlib.import_module("backend.main")
    test_client = TestClient(backend_main.app)
    try:
        yield test_client
    finally:
        test_client.close()


# ---------------------------------------------------------------------------
# Convenience: ensure repo root is on sys.path so ``import backend.*`` works.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
