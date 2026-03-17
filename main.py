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
from typing import Optional

from backend.database import engine, Base, AsyncSessionLocal, Task, User, Group, GroupMember, TaskType
from backend.auth import validate_telegram_data, BOT_TOKEN


bot       = Bot(token=BOT_TOKEN)
dp        = Dispatcher()
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    bot_task = asyncio.create_task(dp.start_polling(bot))
    scheduler.add_job(send_reminders, "cron", hour=8,  minute=0)
    scheduler.add_job(send_reminders, "cron", hour=13, minute=0)
    scheduler.add_job(send_reminders, "cron", hour=19, minute=0)
    scheduler.start()
    yield
    bot_task.cancel()
    scheduler.shutdown()
    await bot.session.close()


app       = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="frontend/templates")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


class GroupCreate(BaseModel):
    name: str

class TaskCreate(BaseModel):
    title:     str
    task_type: str           = "specific_date"
    due_date:  Optional[str] = None
    season:    Optional[str] = None
    group_id:  Optional[int] = None

class TaskUpdate(BaseModel):
    title:    Optional[str] = None
    due_date: Optional[str] = None
    season:   Optional[str] = None


def require_user(request: Request) -> int:
    uid = request.headers.get("X-User-Id")
    if not uid:
        raise HTTPException(status_code=401, detail="Не авторизовано")
    return int(uid)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/login")
async def login_via_telegram(request: Request, db: AsyncSession = Depends(get_db)):
    data      = await request.json()
    user_data = validate_telegram_data(data.get("initData", ""))
    if not user_data:
        raise HTTPException(status_code=401, detail="Невалідні дані Telegram")
    await db.merge(User(
        telegram_id=user_data["id"],
        username=user_data.get("username"),
        first_name=user_data.get("first_name", ""),
    ))
    await db.commit()
    return {"status": "ok", "user_id": user_data["id"], "first_name": user_data.get("first_name", "")}


@app.post("/api/groups")
async def create_group(group: GroupCreate, request: Request, db: AsyncSession = Depends(get_db)):
    user_id   = require_user(request)
    new_group = Group(name=group.name, owner_id=user_id)
    db.add(new_group)
    await db.commit()
    await db.refresh(new_group)
    return {"message": "Група створена", "group_id": new_group.id}


@app.get("/api/groups")
async def get_groups(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    owned_res    = await db.execute(select(Group).where(Group.owner_id == user_id))
    owned_groups = owned_res.scalars().all()
    owned_ids    = {g.id for g in owned_groups}
    member_res   = await db.execute(select(GroupMember).where(GroupMember.user_id == user_id))
    joined_ids   = [m.group_id for m in member_res.scalars().all() if m.group_id not in owned_ids]
    joined_groups = []
    if joined_ids:
        jres          = await db.execute(select(Group).where(Group.id.in_(joined_ids)))
        joined_groups = jres.scalars().all()
    return (
        [{"id": g.id, "name": g.name, "role": "owner"}  for g in owned_groups] +
        [{"id": g.id, "name": g.name, "role": "member"} for g in joined_groups]
    )


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    res     = await db.execute(select(Group).where(Group.id == group_id, Group.owner_id == user_id))
    group   = res.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=403, detail="Тільки власник може видалити групу")
    for m in (await db.execute(select(GroupMember).where(GroupMember.group_id == group_id))).scalars().all():
        await db.delete(m)
    for t in (await db.execute(select(Task).where(Task.group_id == group_id))).scalars().all():
        await db.delete(t)
    await db.delete(group)
    await db.commit()
    return {"message": "Групу видалено"}


@app.get("/api/groups/{group_id}/members")
async def get_group_members(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    res   = await db.execute(select(Group).where(Group.id == group_id))
    group = res.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Групу не знайдено")
    is_owner = group.owner_id == user_id
    if not is_owner:
        mc = await db.execute(
            select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
        )
        if not mc.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Немає доступу")
    rows = await db.execute(
        select(GroupMember, User)
        .join(User, User.telegram_id == GroupMember.user_id)
        .where(GroupMember.group_id == group_id)
    )
    members = [
        {"user_id": r.User.telegram_id, "first_name": r.User.first_name,
         "username": r.User.username, "role": r.GroupMember.role}
        for r in rows.all()
        if r.User.telegram_id != group.owner_id
    ]
    owner_res = await db.execute(select(User).where(User.telegram_id == group.owner_id))
    owner     = owner_res.scalar_one_or_none()
    if owner:
        members.insert(0, {
            "user_id": owner.telegram_id, "first_name": owner.first_name,
            "username": owner.username, "role": "owner",
        })
    return {"group_id": group_id, "name": group.name, "members": members}


@app.delete("/api/groups/{group_id}/members/{member_id}")
async def remove_member(group_id: int, member_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    res = await db.execute(select(Group).where(Group.id == group_id, Group.owner_id == user_id))
    if not res.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Тільки власник може видаляти учасників")
    res = await db.execute(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == member_id)
    )
    member = res.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Учасника не знайдено")
    await db.delete(member)
    await db.commit()
    return {"message": "Учасника видалено"}


@app.post("/api/groups/{group_id}/tasks")
async def create_group_task(group_id: int, task: TaskCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """Створити завдання в групі — власник І всі учасники."""
    user_id = require_user(request)
    g_res   = await db.execute(select(Group).where(Group.id == group_id))
    group   = g_res.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Групу не знайдено")
    is_owner = group.owner_id == user_id
    if not is_owner:
        mc = await db.execute(
            select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
        )
        if not mc.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Ти не є учасником цієї групи")
    due_date = datetime.fromisoformat(task.due_date) if task.due_date else None
    try:
        tt = TaskType(task.task_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Невідомий task_type: {task.task_type}")
    new_task = Task(
        title=task.title, task_type=tt, due_date=due_date, season=task.season,
        user_id=None, group_id=group_id, created_by=user_id,
    )
    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)
    return {"message": "Завдання створено", "task_id": new_task.id}


@app.get("/api/groups/{group_id}/tasks")
async def get_group_tasks(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    g_res   = await db.execute(select(Group).where(Group.id == group_id))
    group   = g_res.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Групу не знайдено")
    is_owner = group.owner_id == user_id
    if not is_owner:
        mc = await db.execute(
            select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
        )
        if not mc.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Немає доступу")
    res   = await db.execute(select(Task).where(Task.group_id == group_id))
    tasks = res.scalars().all()
    return [
        {"id": t.id, "title": t.title, "task_type": t.task_type.value,
         "is_completed": t.is_completed,
         "due_date": t.due_date.isoformat() if t.due_date else None,
         "season": t.season, "created_by": t.created_by}
        for t in tasks
    ]


@app.post("/api/tasks")
async def create_task(task: TaskCreate, request: Request, db: AsyncSession = Depends(get_db)):
    user_id  = require_user(request)
    due_date = datetime.fromisoformat(task.due_date) if task.due_date else None
    try:
        tt = TaskType(task.task_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Невідомий task_type: {task.task_type}")
    new_task = Task(
        title=task.title, task_type=tt, due_date=due_date, season=task.season,
        user_id=user_id, group_id=None, created_by=user_id,
    )
    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)
    return {"message": "Завдання створено", "task_id": new_task.id}


@app.get("/api/tasks")
async def get_tasks(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    res     = await db.execute(select(Task).where(Task.user_id == user_id))
    tasks   = res.scalars().all()
    return [
        {"id": t.id, "title": t.title, "task_type": t.task_type.value,
         "is_completed": t.is_completed,
         "due_date": t.due_date.isoformat() if t.due_date else None,
         "season": t.season, "created_by": t.created_by}
        for t in tasks
    ]


@app.patch("/api/tasks/{task_id}/complete")
async def complete_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = require_user(request)
    res     = await db.execute(select(Task).where(Task.id == task_id))
    task    = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")
    if task.user_id and task.user_id != user_id:
        raise HTTPException(status_code=403, detail="Немає доступу")
    task.is_completed = True
    await db.commit()
    return {"message": "Виконано"}


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, body: TaskUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    """Редагувати може тільки автор завдання (created_by)."""
    user_id = require_user(request)
    res     = await db.execute(select(Task).where(Task.id == task_id))
    task    = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")
    author = task.created_by or task.user_id
    if author != user_id:
        raise HTTPException(status_code=403, detail="Редагувати може тільки автор завдання")
    if body.title    is not None: task.title    = body.title
    if body.due_date is not None: task.due_date = datetime.fromisoformat(body.due_date)
    if body.season   is not None: task.season   = body.season
    await db.commit()
    return {"message": "Завдання оновлено"}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Видалення (включно з виконаними):
    Особисте  → автор.
    Групове   → власник групи.
    """
    user_id = require_user(request)
    res     = await db.execute(select(Task).where(Task.id == task_id))
    task    = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")
    if task.group_id:
        g_res = await db.execute(
            select(Group).where(Group.id == task.group_id, Group.owner_id == user_id)
        )
        if not g_res.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Видаляти групові завдання може тільки власник групи")
    else:
        author = task.created_by or task.user_id
        if author != user_id:
            raise HTTPException(status_code=403, detail="Видаляти може тільки автор")
    await db.delete(task)
    await db.commit()
    return {"message": "Завдання видалено"}


@dp.message(CommandStart())
async def cmd_start(message: Message):
    web_app_url = os.getenv("WEB_APP_URL", "https://your-app.render.com")
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("group_"):
        await handle_group_invite(message, args[1], web_app_url)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Відкрити Task Tracker", web_app=WebAppInfo(url=web_app_url))
    ]])
    await message.answer(
        "👋 Привіт! Я твій Task Tracker.\n\nНатисни кнопку нижче, щоб керувати завданнями:",
        reply_markup=keyboard,
    )


async def handle_group_invite(message: Message, param: str, web_app_url: str):
    try:
        group_id = int(param.replace("group_", ""))
    except ValueError:
        await message.answer("❌ Невалідне посилання.")
        return
    async with AsyncSessionLocal() as db:
        res   = await db.execute(select(Group).where(Group.id == group_id))
        group = res.scalar_one_or_none()
        if not group:
            await message.answer("❌ Групу не знайдено або її видалено.")
            return
        open_btn = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Відкрити групу",
                web_app=WebAppInfo(url=f"{web_app_url}?group={group_id}"))
        ]])
        if group.owner_id == message.from_user.id:
            await message.answer(f"👑 Ти власник групи «{group.name}»!", reply_markup=open_btn)
            return
        await db.merge(User(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name or "",
        ))
        existing = await db.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id  == message.from_user.id,
            )
        )
        if existing.scalar_one_or_none():
            await message.answer(f"✅ Ти вже учасник групи «{group.name}»!", reply_markup=open_btn)
            return
        db.add(GroupMember(group_id=group_id, user_id=message.from_user.id))
        await db.commit()
        await message.answer(f"🎉 Ти успішно приєднався до групи «{group.name}»!", reply_markup=open_btn)


async def send_reminders():
    async with AsyncSessionLocal() as db:
        res   = await db.execute(select(Task).where(Task.is_completed == False))  # noqa: E712
        tasks = res.scalars().all()
        user_tasks: dict[int, list[str]] = {}
        for task in tasks:
            if task.user_id:
                user_tasks.setdefault(task.user_id, []).append(task.title)
        for uid, t_list in user_tasks.items():
            text = "🔔 Нагадування! Невиконані завдання:\n\n" + "\n".join(f"• {t}" for t in t_list)
            try:
                await bot.send_message(chat_id=uid, text=text)
            except Exception as e:
                print(f"[Reminder] Помилка для {uid}: {e}")
