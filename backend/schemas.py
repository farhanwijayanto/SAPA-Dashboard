from pydantic import BaseModel, ConfigDict
from datetime import date, datetime
from typing import List, Optional

class UserBase(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str
    permissions: List[str] = []

class User(UserBase):
    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    id: Optional[int] = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None


class ProfileUpdateResponse(BaseModel):
    user: User
    access_token: Optional[str] = None
    token_type: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class EmployeeBase(BaseModel):
    id: str
    name: str
    dob: date
    division: str
    position: str

class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    dob: date
    division: str
    position: str

class Employee(EmployeeBase):
    class Config:
        from_attributes = True

class AttendanceBase(BaseModel):
    employee_id: str
    direction: str
    status: str
    reason: Optional[str] = None

class AttendanceCreate(AttendanceBase):
    pass

class Attendance(AttendanceBase):
    timestamp: datetime
    employee: Employee

class LoginRequest(BaseModel):
    username: str
    password: str
    user_id: Optional[int] = None

class Token(BaseModel):
    access_token: str
    token_type: str
    role: str
    user_id: int
    permissions: List[str] = []


class ManagedUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    password: str
    role: str
    permissions: List[str] = []

class TokenData(BaseModel):
    username: Optional[str] = None
    role: Optional[str] = None

class AuditEventCreate(BaseModel):
    event_type: str
    message: str


class EdgeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_valid: bool
    employee_id: Optional[str] = None
    message: Optional[str] = None


class EdgeStatus(BaseModel):
    ts: datetime
    is_valid: Optional[bool] = None
    employee_id: Optional[str] = None
    message: Optional[str] = None
    stale: bool
    age_ms: int


class FaceMatchEvent(BaseModel):
    """Result from edge AI face recognition."""
    model_config = ConfigDict(extra="forbid")
    is_valid: bool
    employee_id: Optional[str] = None
    direction: Optional[str] = "in"
    confidence: Optional[float] = None
    message: Optional[str] = None
    edge_key: Optional[str] = None


class RoleBase(BaseModel):
    division: str
    position: str
    description: Optional[str] = None


class RoleCreate(RoleBase):
    model_config = ConfigDict(extra="forbid")


class Role(RoleBase):
    id: int

    class Config:
        from_attributes = True


class RoleStats(BaseModel):
    id: int
    division: str
    position: str
    description: Optional[str] = None
    total: int
    active_today: int
    inactive_today: int
