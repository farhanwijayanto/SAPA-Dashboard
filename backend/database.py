from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from pathlib import Path

_env_db_url = os.getenv("DATABASE_URL")
if _env_db_url:
    SQLALCHEMY_DATABASE_URL = _env_db_url
else:
    default_db_path = (Path(__file__).resolve().parent.parent / "attendance.db").as_posix()
    SQLALCHEMY_DATABASE_URL = f"sqlite:///{default_db_path}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
