"""Database seed for SAPA.

Behavior:
- Only creates `admin` and `manager` users if they do NOT already exist.
- Existing users are left untouched (passwords are NEVER overwritten on reseed).
- Initial passwords come from env vars SAPA_ADMIN_PASSWORD / SAPA_MANAGER_PASSWORD
  when set; otherwise fall back to dev defaults `admin123` / `manager123`.
  The CLI prints which source was used.

This script is idempotent and safe to call multiple times, but in production
it is gated by SAPA_RUN_SEED=1 in `entrypoint.sh` so it only runs on demand.
"""

import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

try:
    from database import SessionLocal, engine
    from models import Base, User, Employee
    from auth import encrypt_password
except ImportError:
    from .database import SessionLocal, engine
    from .models import Base, User, Employee
    from .auth import encrypt_password

from datetime import date
import random

DEV_DEFAULT_ADMIN_PASSWORD = "admin123"
DEV_DEFAULT_MANAGER_PASSWORD = "manager123"


def _get_or_generate_user_id(db, username: str) -> int:
    """Get existing user ID or generate a new random 6-digit one"""
    existing_user = db.query(User).filter(User.username == username).first()
    if existing_user:
        return existing_user.id

    # Generate random 6-digit ID (100000-999999)
    for _ in range(50):
        candidate = random.randint(100000, 999999)
        exists = db.query(User).filter(User.id == candidate).first()
        if not exists:
            return candidate
    raise RuntimeError(f"Could not generate unique user ID for {username}")


def _resolve_password(env_var: str, dev_default: str) -> tuple[str, str]:
    value = os.getenv(env_var)
    if value:
        return value, "env"
    return dev_default, "dev-default"


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        manager_password, manager_source = _resolve_password(
            "SAPA_MANAGER_PASSWORD", DEV_DEFAULT_MANAGER_PASSWORD
        )
        admin_password, admin_source = _resolve_password(
            "SAPA_ADMIN_PASSWORD", DEV_DEFAULT_ADMIN_PASSWORD
        )

        manager = db.query(User).filter(User.username == "manager").first()
        if manager is None:
            manager_id = _get_or_generate_user_id(db, "manager")
            manager_cipher, manager_iv = encrypt_password(manager_password)
            db.add(
                User(
                    id=manager_id,
                    username="manager",
                    email="manager@sapa.local",
                    password_ciphertext=manager_cipher,
                    password_iv=manager_iv,
                    role="manager",
                )
            )
            print(
                f"Manager created -> username=manager id={manager_id} "
                f"password_source={manager_source}"
            )
        else:
            print(
                f"Manager already exists (id={manager.id}); password NOT overwritten."
            )

        admin = db.query(User).filter(User.username == "admin").first()
        if admin is None:
            admin_id = _get_or_generate_user_id(db, "admin")
            admin_cipher, admin_iv = encrypt_password(admin_password)
            db.add(
                User(
                    id=admin_id,
                    username="admin",
                    email="admin@sapa.local",
                    password_ciphertext=admin_cipher,
                    password_iv=admin_iv,
                    role="admin",
                )
            )
            print(
                f"Admin created   -> username=admin   id={admin_id} "
                f"password_source={admin_source}"
            )
        else:
            print(f"Admin already exists (id={admin.id}); password NOT overwritten.")

        if db.query(Employee).count() == 0:
            db.add(
                Employee(
                    id=str(random.randint(100000, 999999)),
                    name="John Doe",
                    dob=date(1990, 1, 1),
                    division="Production",
                    position="Operator",
                )
            )
            print("Seeded sample Employee 'John Doe'.")

        db.commit()
        print("Database seed complete.")

        if manager_source == "dev-default" or admin_source == "dev-default":
            print(
                "WARNING: at least one default dev password was used. "
                "Set SAPA_ADMIN_PASSWORD and SAPA_MANAGER_PASSWORD before "
                "running seed in production, or change the password "
                "immediately after first login."
            )
    finally:
        db.close()


if __name__ == "__main__":
    seed()
