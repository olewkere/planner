from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Enum
import enum, os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class TaskType(enum.Enum):
    DAILY = "daily"
    SPECIFIC_DATE = "specific_date"
    SEASONAL = "seasonal"

class User(Base):
    __tablename__ = "users"
    telegram_id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=True)
    first_name = Column(String)

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.telegram_id"))

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    task_type = Column(Enum(TaskType), default=TaskType.SPECIFIC_DATE)
    due_date = Column(DateTime, nullable=True)
    season = Column(String, nullable=True)
    is_completed = Column(Boolean, default=False)
    user_id = Column(Integer, ForeignKey("users.telegram_id"), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    
class GroupMember(Base):
    __tablename__ = "group_members"
    id       = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    user_id  = Column(Integer, ForeignKey("users.telegram_id"), nullable=False)
    role     = Column(String, default="member")  # "owner" | "member"
    joined_at = Column(DateTime, default=datetime.utcnow)
