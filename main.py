import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.filters import CommandStart

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from pydantic import BaseModel

from backend.database import engine, Base, AsyncSessionLocal, Task, User, Group, TaskType
from backend.auth import validate_telegram_data, BOT_TOKEN


# ── Ініціалізація ──────────────────────────────────────────────────────────

bot       = Bot(token=BOT_TOKEN)
dp        = Dispatcher()
scheduler = AsyncIOScheduler()


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    bot_task = asyncio.create_task(dp.start_polling(bot))

    scheduler.add_job(send_reminders, "cron", hour=8,  minute=0)
    scheduler.add_job(send_reminders, "cron", hour=13, minute=0)
    scheduler.add_job(send_reminders, "cron", hour=19, minute=0)
    scheduler.start()

    yield  # сервер працює тут

    # SHUTDOWN
    bot_task.cancel()
    scheduler.shutdown()
    await bot.session.close()


# ── FastAPI app ────────────────────────────────────────────────────────────

app       = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="frontend/templates")


# ── DB dependency ──────────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ── Pydantic schemas ───────────────────────────────────────────────────────

class GroupCreate(BaseModel):
    name: str

class TaskCreate(BaseModel):
    title: str
    task_type: str  = "specific_date"  # "daily" | "specific_date" | "seasonal"
    due_date:  str  | None = None      # ISO-рядок "2025-12-31"
    season:    str  | None = None      # "winter" | "spring" | "summer" | "autumn"
    group_id:  int  | None = None


# ── Helper ─────────────────────────────────────────────────────────────────

def require_user(request: Request) -> int:
    """Дістає user_id з заголовка X-User-Id або кидає 401."""
    uid = request.headers.get("X-User-Id")
    if not uid:
        raise HTTPException(status_code=401, detail="Не авторизовано")
    return int(uid)


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── AUTH ───────────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login_via_telegram(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Авторизація через Telegram initData.
    Повертає user_id — фронтенд зберігає його і передає
    у заголовку X-User-Id при кожному наступному запиті.
    """
    data      = await request.json()
    user_data = validate_telegram_data(data.get("initData", ""))

    if not user_data:
        raise HTTPException(status_code=401, detail="Невалідні дані Telegram")

    user = User(
        telegram_id=user_data["id"],
        username=user_data.get("username"),
        first_name=user_data.get("first_name", ""),
    )
    await db.merge(user)
    await db.commit()

    return {
        "status":     "ok",
        "user_id":    user_data["id"],
        "first_name": user_data.get("first_name", ""),
    }


# ── GROUPS ─────────────────────────────────────────────────────────────────

@app.post("/api/groups")
async def create_group(
    group:   GroupCreate,
    request: Request,
    db:      AsyncSession = Depends(get_db),
):
    user_id   = require_user(request)
    new_group = Group(name=group.name, owner_id=user_id)
    db.add(new_group)
    await db.commit()
    await db.refresh(new_group)
    return {"message": "Група створена", "group_id": new_group.id}


@app.get("/api/groups")
async def get_groups(request: Request, db: AsyncSession = Depends(get_db)):
    """Повертає всі групи де поточний юзер є власником."""
    user_id = require_user(request)
    result  = await db.execute(
        select(Group).where(Group.owner_id == user_id)
    )
    groups = result.scalars().all()
    return [{"id": g.id, "name": g.name} for g in groups]


# ── TASKS ──────────────────────────────────────────────────────────────────

@app.post("/api/tasks")
async def create_task(
    task:    TaskCreate,
    request: Request,
    db:      AsyncSession = Depends(get_db),
):
    user_id  = require_user(request)
    due_date = datetime.fromisoformat(task.due_date) if task.due_date else None

    try:
        task_type_enum = TaskType(task.task_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Невідомий task_type: {task.task_type}")

    new_task = Task(
        title=task.title,
        task_type=task_type_enum,
        due_date=due_date,
        season=task.season,
        user_id=user_id,
        group_id=task.group_id,
    )
    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)
    return {"message": "Завдання створено", "task_id": new_task.id}


@app.get("/api/tasks")
async def get_tasks(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    result  = await db.execute(
        select(Task).where(Task.user_id == user_id)
    )
    tasks = result.scalars().all()
    return [
        {
            "id":           t.id,
            "title":        t.title,
            "task_type":    t.task_type.value,
            "is_completed": t.is_completed,
            "due_date":     t.due_date.isoformat() if t.due_date else None,
            "season":       t.season,
        }
        for t in tasks
    ]


@app.patch("/api/tasks/{task_id}/complete")
async def complete_task(
    task_id: int,
    request: Request,
    db:      AsyncSession = Depends(get_db),
):
    user_id = require_user(request)
    result  = await db.execute(
        select(Task).where(Task.id == task_id, Task.user_id == user_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")

    task.is_completed = True
    await db.commit()
    return {"message": "Завдання виконано"}


# ── Telegram bot ───────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    web_app_url = os.getenv("WEB_APP_URL", "https://your-app.render.com")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="📋 Відкрити Task Tracker",
                web_app=WebAppInfo(url=web_app_url),
            )
        ]]
    )
    await message.answer(
        "👋 Привіт! Я твій Task Tracker.\n\n"
        "Натисни кнопку нижче, щоб керувати завданнями:",
        reply_markup=keyboard,
    )


# ── Reminders (APScheduler) ────────────────────────────────────────────────

async def send_reminders():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Task).where(Task.is_completed == False)  # noqa: E712
        )
        tasks = result.scalars().all()

        user_tasks: dict[int, list[str]] = {}
        for task in tasks:
            if task.user_id:
                user_tasks.setdefault(task.user_id, []).append(task.title)

        for user_id, t_list in user_tasks.items():
            text  = "🔔 Нагадування! Невиконані завдання:\n\n"
            text += "\n".join(f"• {t}" for t in t_list)
            try:
                await bot.send_message(chat_id=user_id, text=text)
            except Exception as e:
                print(f"[Reminder] Помилка для {user_id}: {e}")