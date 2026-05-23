from sqlalchemy import Column, Integer, String, Date, Text

# Support both relative and absolute imports
try:
    from .database import Base
except ImportError:
    from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    full_name = Column(String, nullable=True)
    avatar_filename = Column(String, nullable=True)
    permissions_json = Column(Text, nullable=True)
    password_ciphertext = Column(String)
    password_iv = Column(String)
    role = Column(String) # manager or admin

class Employee(Base):
    __tablename__ = "employees"

    id = Column(String, primary_key=True, index=True) # Employee ID
    name = Column(String)
    dob = Column(Date)
    division = Column(String)
    position = Column(String)


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    division = Column(String, index=True)
    position = Column(String, index=True)
    description = Column(String, nullable=True)
