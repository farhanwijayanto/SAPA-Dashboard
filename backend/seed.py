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

def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Get or generate manager ID
        manager_id = _get_or_generate_user_id(db, "manager")
        
        manager_cipher, manager_iv = encrypt_password("manager123")
        manager = (
            db.query(User)
            .filter(User.username == "manager")
            .first()
        )
        if manager:
            # Update existing manager (keep same ID)
            manager.email = "manager@sapa.local"
            manager.username = "manager"
            manager.password_ciphertext = manager_cipher
            manager.password_iv = manager_iv
            manager.role = "manager"
        else:
            # Create new manager with generated ID
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

        # Get or generate admin ID
        admin_id = _get_or_generate_user_id(db, "admin")
        
        admin_cipher, admin_iv = encrypt_password("admin123")
        admin = (
            db.query(User)
            .filter(User.username == "admin")
            .first()
        )
        if admin:
            # Update existing admin (keep same ID)
            admin.email = "admin@sapa.local"
            admin.username = "admin"
            admin.password_ciphertext = admin_cipher
            admin.password_iv = admin_iv
            admin.role = "admin"
        else:
            # Create new admin with generated ID
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

        db.commit()
        print("Database seeded successfully.")
        print(f"Manager login -> username=manager id={manager_id} password=manager123")
        print(f"Admin login   -> username=admin   id={admin_id} password=admin123")
    finally:
        db.close()

if __name__ == "__main__":
    seed()
